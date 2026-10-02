#!/usr/bin/env python3
"""
AI-readability + search-visibility auditor built on TinyFish Search and Fetch.

    export TINYFISH_API_KEY=...
    python ai_seo_auditor.py https://example.com/some-page -q "target query" -o report

Fetch tells us what an AI tool actually extracts from the page.
Search tells us how (and whether) the page shows up.
The report connects the two: every gap is tied to a visibility consequence.
"""
from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
import difflib
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from html.parser import HTMLParser
from urllib.parse import urlparse, parse_qsl, urlencode

import requests

SEARCH_URL = "https://api.search.tinyfish.ai"
FETCH_URL = "https://api.fetch.tinyfish.ai"
UA = "Mozilla/5.0 (compatible; ai-seo-auditor/0.1)"

# bot token -> what blocking it costs you. Verify against vendor docs; this list drifts.
BOTS = {
    "Googlebot": "search", "Bingbot": "search",
    "OAI-SearchBot": "search", "PerplexityBot": "search", "Claude-SearchBot": "search",
    "ChatGPT-User": "user", "Claude-User": "user", "Perplexity-User": "user",
    "GPTBot": "training", "ClaudeBot": "training", "Google-Extended": "training",
    "Applebot-Extended": "training", "CCBot": "training",
}
STOP = set("a an and are as at be by for from how in is it of on or that the to what when where which who why with your you".split())
PEN = {"high": 15, "med": 7, "low": 3, "info": 0}


# --------------------------------------------------------------------------- TinyFish client
class TinyFish:
    """Thin REST wrapper. Logs every call so the report can show its own provenance."""

    def __init__(self, api_key: str):
        self.s = requests.Session()
        self.s.headers["X-API-Key"] = api_key
        self.calls: list[tuple[str, str]] = []

    def search(self, query: str, **params) -> list[dict]:
        r = self.s.get(SEARCH_URL, params={"query": query, **params}, timeout=30)
        r.raise_for_status()
        self.calls.append(("search", f"{query} (page {params.get('page', 0)})"))
        return r.json().get("results", [])

    def fetch(self, urls: list[str], **body) -> tuple[dict, dict]:
        payload = {"urls": urls, "format": "markdown", "ttl": 0, **body}  # ttl=0 -> live, not cached
        r = self.s.post(FETCH_URL, json=payload, timeout=150)
        r.raise_for_status()
        data = r.json()
        self.calls.append(("fetch", f"{payload['format']} " + ", ".join(urls)))
        return ({x["url"]: x for x in data.get("results", [])},
                {e["url"]: e for e in data.get("errors", [])})


# --------------------------------------------------------------------------- small helpers
@dataclass
class Finding:
    area: str      # access | content | metadata | visibility
    sev: str       # high | med | low | info
    title: str
    evidence: str
    fix: str
    affects: str   # ai | search | both
    ties: str = ""  # how this readability issue turns into a visibility issue


def norm(u: str) -> str:
    """Compare URLs ignoring scheme, www, trailing slash and tracking params."""
    p = urlparse(u.strip())
    host = p.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    q = urlencode([(k, v) for k, v in parse_qsl(p.query) if not re.match(r"(utm_|fbclid|gclid|mc_)", k)])
    return host + (p.path.rstrip("/") or "") + (("?" + q) if q else "")


def tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (s or "").lower())


def clean(s) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ").replace("\u00b6", "")).strip()


def tidy_snippet(s) -> str:
    """Search snippets end in ' . . . · ·' style debris; keep the text, drop the trailing separators."""
    return re.sub(r"(?:\s*[\u00b7\u2022|])+\s*$", "", clean(s))


CHALLENGE_TITLES = {"client challenge", "just a moment...", "just a moment", "attention required! | cloudflare", "access denied",
                    "are you a robot?", "robot check", "verify you are human", "security check", "checking your browser",
                    "pardon our interruption"}
CHALLENGE_RX = re.compile(r"checking (if the site connection is secure|your browser)|verify(ing)? you are (a )?human|enable javascript and cookies|"
                          r"javascript is disabled in your browser|please enable javascript to proceed|cloudflare ray id|"
                          r"complete the security check|captcha|unusual traffic", re.I)


def looks_like_challenge(title: str, text: str, words: int) -> bool:
    """A bot-check interstitial (Cloudflare, PyPI 'Client Challenge'...) is not the page's real content."""
    return clean(title).lower() in CHALLENGE_TITLES or (words < 150 and bool(CHALLENGE_RX.search(text or "")))


def best_h1(headings, title: str, fallback: str = "") -> str:
    """Several <h1> can exist (nav/logo/demo text). Prefer the one that overlaps the <title> most; first one on ties."""
    cands = [t for lvl, t in headings if lvl == 1 and t]
    if not cands:
        return fallback
    tt = set(tokens(title))

    def score(t):
        ts = set(tokens(t))
        return len(ts & tt) / len(ts) if ts else 0.0

    best = max(cands, key=score)  # max() keeps the first of equal scores
    return fallback if score(best) == 0 and fallback else best


def first_skip(heads):
    """First pair of consecutive headings whose level jumps by more than one, or None."""
    return next((((a, ta), (b, tb)) for (a, ta), (b, tb) in zip(heads, heads[1:]) if b - a > 1), None)


def decode_body(r) -> str:
    """requests falls back to ISO-8859-1 when the server names no charset, which garbles UTF-8 (em dash -> 'â\x80\x94')."""
    if re.search(r"charset=", r.headers.get("Content-Type", ""), re.I):
        return r.text
    try:
        return r.content.decode("utf-8")
    except UnicodeDecodeError:
        return r.content.decode("cp1252", errors="replace")


def term_hits(terms: list[str], text: str):
    """Share of query terms present in text. 'seo' also matches 'Search Engine Optimization'."""
    if not terms:
        return None
    t = clean(text).lower()
    initials = "".join(w[0] for w in tokens(t))
    return sum(1 for x in terms if x in t or (3 <= len(x) <= 6 and x in initials)) / len(terms)


def key_terms(q: str) -> list[str]:
    return [t for t in tokens(q) if t not in STOP]


def sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def count_words(md: str) -> int:
    return len(re.findall(r"\w+", re.sub(r"\]\([^)]*\)", " ", md or "")))


def derive_query(title: str, h1: str) -> str:
    parts = re.split(r"\s[|\-–—:]\s", h1 or title or "")
    return max(parts, key=len).strip() if parts else ""


def md_stats(md: str):
    lines = (md or "").splitlines()
    heads = [(len(m.group(1)), clean(m.group(2))) for l in lines if (m := re.match(r"^(#{1,6})\s+(.*\S)", l))]
    tables = sum(1 for l in lines if "|" in l and re.match(r"^\s*\|?\s*:?-{3,}", l))
    lists = sum(1 for l in lines if re.match(r"^\s*([-*+]|\d+[.)])\s+\S", l))
    return heads, tables, lists


def profile(r: dict, terms: list[str], rank: int | None = None) -> dict:
    """Same yardstick for the audited page and for the pages that outrank it."""
    md = r.get("text") or ""
    heads, tables, lists = md_stats(md)
    title = clean(r.get("title"))
    h1 = next((t for lvl, t in heads if lvl == 1), "")
    first = " ".join(md.split()[:120])

    def hit(s: str):
        return term_hits(terms, s)

    return {
        "url": r.get("final_url") or r.get("url"), "rank": rank, "title": title, "h1": h1,
        "words": count_words(md), "h2": sum(1 for l, _ in heads if l == 2), "tables": tables, "lists": lists,
        "questions": sum(1 for _, t in heads if t.endswith("?") or re.match(r"(what|how|why|when|can|is|does|should)\b", t, re.I)),
        "published": r.get("published_date"), "author": r.get("author"),
        "t_title": hit(title), "t_h1": hit(h1), "t_first": hit(first), "t_body": hit(md),
    }


def avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    return xs[len(xs) // 2] if xs else None


# --------------------------------------------------------------------------- robots.txt
def parse_robots(txt: str):
    # Fetch returns markdown, so `User-Agent: *` arrives as `User-Agent: \*`
    txt = re.sub(r"\\([\\`*_{}\[\]()#+\-.!$])", r"\1", txt)
    groups, cur, last_ua, sitemaps = [], None, False, []
    for raw in txt.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            if cur is None or not last_ua:
                cur = {"agents": [], "rules": []}
                groups.append(cur)
            cur["agents"].append(v.lower())
            last_ua = True
            continue
        last_ua = False
        if k in ("allow", "disallow") and cur is not None:
            cur["rules"].append((k, v))
        elif k == "sitemap":
            sitemaps.append(v)
    return groups, sitemaps


def _rx(pat: str):
    anchored = pat.endswith("$")
    pat = pat[:-1] if anchored else pat
    return re.compile("^" + re.escape(pat).replace(r"\*", ".*") + ("$" if anchored else ""))


def robots_allows(groups, bot: str, path: str) -> bool:
    bot = bot.lower()
    gs = [g for g in groups if bot in g["agents"]] or [g for g in groups if "*" in g["agents"]]
    best_len, allowed = -1, True
    for g in gs:
        for kind, pat in g["rules"]:
            if pat and _rx(pat).match(path):
                if len(pat) > best_len or (len(pat) == best_len and kind == "allow"):
                    best_len, allowed = len(pat), kind == "allow"
    return allowed


# --------------------------------------------------------------------------- HTML parsing
class Outline(HTMLParser):
    """Headings and <img> tags out of Fetch's cleaned html (include_selectors)."""

    def __init__(self):
        super().__init__()
        self.images, self.headings = [], []
        self._h, self._buf = None, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if re.fullmatch(r"h[1-6]", tag):
            self._h, self._buf = int(tag[1]), []
        elif tag == "img":
            decorative = a.get("role") in ("presentation", "none") or a.get("aria-hidden") == "true"
            self.images.append({"src": a.get("src") or a.get("data-src") or "", "alt": a.get("alt"), "decorative": decorative})

    def handle_data(self, d):
        if self._h:
            self._buf.append(d)

    def handle_endtag(self, tag):
        if self._h and tag == f"h{self._h}":
            self.headings.append((self._h, clean("".join(self._buf))))
            self._h = None


class RawPage(HTMLParser):
    """What a crawler that does NOT run JavaScript sees in the first HTML response."""
    SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.meta, self.jsonld, self.text = "", {}, [], []
        self._stack, self._in_title, self._ld, self._buf = [], False, False, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            k = (a.get("name") or a.get("property") or "").lower()
            if k and "content" in a:
                self.meta[k] = a["content"]
        elif tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._ld, self._buf = True, []
        if tag in self.SKIP:
            self._stack.append(tag)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "script" and self._ld:
            self.jsonld.append("".join(self._buf))
            self._ld = False
        if self._stack and tag == self._stack[-1]:
            self._stack.pop()

    def handle_data(self, d):
        if self._in_title:
            self.title += d
        if self._ld:
            self._buf.append(d)
        if not self._stack:
            self.text.append(d)


def raw_view(url: str) -> dict:
    """Plain HTTP GET. Not a TinyFish call: Fetch renders JS and hides <script> tags, so this is
    the only way to see JSON-LD and to measure how much text depends on JavaScript."""
    r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
    p = RawPage()
    p.feed(decode_body(r))
    return {"status": r.status_code, "x_robots": r.headers.get("X-Robots-Tag", ""), "title": p.title.strip(),
            "meta": p.meta, "jsonld": p.jsonld, "text": " ".join(p.text)}


def ld_summary(blocks: list[str]):
    types, bad, gaps = [], 0, []
    need = {"Article": ["headline", "datePublished", "author"], "BlogPosting": ["headline", "datePublished", "author"],
            "NewsArticle": ["headline", "datePublished", "author"], "Product": ["name", "offers"],
            "FAQPage": ["mainEntity"], "Organization": ["name"]}

    def walk(o, top):
        if isinstance(o, list):
            for x in o:
                walk(x, top)
        elif isinstance(o, dict):
            ts = o.get("@type")
            for t in (ts if isinstance(ts, list) else [ts] if ts else []):
                types.append(t)
                if top and t in need and (miss := [k for k in need[t] if k not in o]):
                    gaps.append((t, miss))
            for k, v in o.items():
                if k == "@graph":
                    walk(v, top)
                elif isinstance(v, (dict, list)):
                    walk(v, False)

    for b in blocks:
        try:
            walk(json.loads(b), True)
        except Exception:
            bad += 1
    return types, bad, gaps


def coverage(rendered_md: str, raw_text: str, n=12, w=8):
    """Sample n windows of w words from the rendered page; how many exist in the raw HTML text?
    Returns (share present, readable passages that are missing)."""
    toks = tokens(re.sub(r"\]\([^)]*\)", " ", rendered_md))
    if len(toks) < w * 3:
        return None, []
    step = max(1, (len(toks) - w) // n)
    wins = [toks[i:i + w] for i in range(0, len(toks) - w, step)][:n]
    hay = "".join(tokens(raw_text))  # spacing-insensitive on both sides
    missing = [" ".join(x) for x in wins if "".join(x) not in hay]
    return 1 - len(missing) / len(wins), missing


# --------------------------------------------------------------------------- search-side helpers
def snippet_source(snip: str, desc: str, body: str) -> str:
    st = set(tokens(snip)) - STOP
    if not st:
        return "unknown"
    ov = lambda t: len(st & set(tokens(t))) / len(st)
    if desc and ov(desc) >= 0.7:
        return "meta description"
    return "page body" if ov(body) >= 0.7 else "unknown"


def title_rewritten(serp_title: str, title: str, h1: str) -> bool:
    s = re.sub(r"[.…\s]+$", "", serp_title or "").lower()
    cands = [c.lower() for c in (title, h1) if c]
    if not s or not cands:
        return False
    if any(c in s or s in c for c in cands):  # truncated or brand appended: not a rewrite
        return False
    return max(sim(s, c) for c in cands) < 0.5


def suggest_title(h1: str, title: str, site: str) -> str:
    core = (h1 or title).strip()
    room = 60 - (len(site) + 3 if site else 0)
    if len(core) > room:
        core = core[:room].rsplit(" ", 1)[0]
    return f"{core} | {site}" if site else core


def jsonld_skeleton(p: dict, desc: str, meta: dict, final_url: str, article: bool) -> str:
    og = meta.get("og") or {}
    img = og.get("image")
    d = {"@context": "https://schema.org", "@type": "Article" if article else "WebPage",
         "headline" if article else "name": p["h1"] or p["title"], "description": desc or "WRITE A 120-160 CHAR SUMMARY",
         "url": final_url}
    if img:
        d["image"] = img[0] if isinstance(img, list) else img
    if article:
        d["datePublished"] = p["published"] or "YYYY-MM-DD"
        d["author"] = {"@type": "Person", "name": p["author"] or "AUTHOR NAME"}
    return json.dumps(d, indent=2)


def select_fix_first(findings: list[Finding]) -> list[str]:
    """Selects the top 3 most critical findings for the 'Fix first' section."""
    if not findings:
        return []

    # 1. Filter optional/informational
    filtered = [f for f in findings if f.title not in ("No llms.txt", "Meta description length")]

    # 2. Group Title + H1 query term findings
    merged = []
    skip_next = False
    for i, f in enumerate(filtered):
        if skip_next:
            skip_next = False
            continue

        if i + 1 < len(filtered):
            f1, f2 = f, filtered[i+1]
            if {f1.title, f2.title} == {"Query terms missing from your title tag", "Query terms missing from your H1"}:
                merged.append(Finding(f1.area, f1.sev, "Work the query terms into your title and H1", f1.evidence, f1.fix, f1.affects, f1.ties))
                skip_next = True
                continue
        merged.append(f)

    # 3. Order by severity, then 'both'
    order = {"high": 0, "med": 1, "low": 2, "info": 3}
    merged.sort(key=lambda f: (order.get(f.sev, 3), 0 if f.affects == "both" else 1))

    return [f.title for f in merged[:3]]


# --------------------------------------------------------------------------- the audit
def audit(url: str, query: str | None, tf, do_raw=True, country: str | None = None, raw_fn=None) -> dict:
    raw_fn = raw_fn or raw_view
    F: list[Finding] = []

    def add(*a, **k):
        F.append(Finding(*a, **k))

    p = urlparse(url)
    host, origin = p.netloc.lower(), f"{p.scheme}://{p.netloc}"

    # ---- FETCH 1: the page as an AI tool extracts it (rendered, live)
    got, errs = tf.fetch([url], links=True, image_links=True, page_metadata=True)
    page = next(iter(got.values()), None)
    e0 = next(iter(errs.values()), {})
    if page is None and (e0.get("status") in (404, 410) or e0.get("error") == "page_not_found"):
        near = []
        try:
            near = [r["url"] for r in tf.search(f"site:{host} {' '.join(tokens(p.path)[-4:])}")[:3]]
        except Exception:  # noqa: BLE001
            pass
        f404 = Finding("access", "high", "This URL does not exist (404)", f"TinyFish Fetch: {e0.get('error')} (HTTP {e0.get('status')}).",
                       "Check the URL for typos. If the page moved or was deleted, add a 301 redirect to its replacement."
                       + (f" Closest pages the search index knows on {host}: {', '.join(near)}." if near else ""), "both",
                       "Nothing at this URL can be indexed, quoted or ranked, and links pointing here are wasted.")
        return {"status": "not_found", "url": url, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "scores": {"ai_readability": None, "search_visibility": None}, "findings": [asdict(f404)], "calls": tf.calls}
    readable = bool(page and (page.get("text") or "").strip())
    if page is None or not readable:
        e = next(iter(errs.values()), {})
        add("access", "high", "AI tools cannot extract any text from this page",
            f"TinyFish Fetch returned {'an error: ' + str(e.get('error')) + ' (HTTP ' + str(e.get('status')) + ')' if page is None else 'an empty body'}.",
            "Check for bot blocking (WAF/Cloudflare rules), login/consent walls, or content rendered only after user interaction. "
            "Test with curl and a normal browser UA, then allow verified AI crawlers.", "both",
            "A page that AI fetchers cannot read cannot be quoted or cited by them, and may also be missing from indexes built from the same crawl.")
        page = page or {}
    md = page.get("text") or ""
    meta = page.get("page_metadata") or {}
    final_url = page.get("final_url") or url
    title = clean(page.get("title"))
    if readable and looks_like_challenge(title, md, count_words(md)):
        add("access", "high", "Fetch was served a bot-challenge page, not your content",
            f"Title {title!r}, {count_words(md)} words, with challenge wording in the body.",
            "Allow verified crawlers through the WAF/bot protection for this path, then re-run. Until then the audit below describes the challenge page.", "both",
            "AI fetchers that hit the same wall see no content to quote or cite.")
    h1 = profile(page, [])["h1"]
    path = urlparse(final_url).path or "/"
    if urlparse(final_url).query:
        path += "?" + urlparse(final_url).query

    is_article = (meta.get("og") or {}).get("type") == "article" or bool(re.search(r"/(blog|news|articles?|posts?)/", path))

    # ---- FETCH 2: real <h1>-<h6> tags and images with alt text (Fetch's cleaned html, scoped by selector)
    images, dom_heads = [], []
    if readable:
        g2, e2 = tf.fetch([url], format="html", include_selectors=["h1", "h2", "h3", "h4", "h5", "h6", "img"], links=False, image_links=False, page_metadata=False)
        if g2:
            o = Outline()
            o.feed(next(iter(g2.values())).get("text") or "")
            images, dom_heads = o.images, o.headings
    h1 = best_h1(dom_heads, title, h1)

    # ---- raw GET (supplement): JSON-LD, meta description, X-Robots-Tag, JS dependence
    raw, raw_err = None, None
    if do_raw:
        try:
            raw = raw_fn(final_url)
        except Exception as ex:  # noqa: BLE001
            raw_err = str(ex)

    desc_src = "raw HTML" if raw else "TinyFish Fetch"
    desc = raw["meta"].get("description", "") if raw else (page.get("description") or "")

    # ---- query: given, or derived from the page itself
    query_src = "given"
    if not query:
        query, query_src = derive_query(title, h1), "derived from the page's own H1/title (tests findability by name only; pass -q for a real keyword test)"
    terms = key_terms(query)
    me = profile(page, terms)
    me["h1"], me["t_h1"] = h1, term_hits(terms, h1)

    # ---- FETCH 3: site-level files AI crawlers look for
    site, _ = tf.fetch([origin + "/robots.txt", origin + "/llms.txt"], links=False, image_links=False, page_metadata=False)
    robots_txt = (site.get(origin + "/robots.txt") or {}).get("text")
    llms_txt = (site.get(origin + "/llms.txt") or {}).get("text")

    def soft(t):  # single-page apps often answer any path with the home page: not a real file
        t = (t or "").strip()
        return not t or t[:200] == md.strip()[:200] or bool(re.match(r"<(!doctype|html)", t, re.I))

    if soft(robots_txt) or not re.search(r"(?im)^\s*user-agent\s*:", robots_txt):
        robots_txt = None
    if soft(llms_txt):
        llms_txt = None
    groups, smaps = parse_robots(robots_txt) if robots_txt else ([], [])
    blocked = {b: not robots_allows(groups, b, path) for b in BOTS} if robots_txt else {}

    # sitemap membership
    sm_urls = smaps or [origin + "/sitemap.xml"]
    sm_found, sm_checked = None, 0
    sm_got, sm_err = tf.fetch(sm_urls[:2], links=False, image_links=False, page_metadata=False)
    keys = {norm(url), norm(final_url)} | ({norm(meta["canonical"])} if meta.get("canonical") else set())
    for r in sm_got.values():
        urls_in = re.findall(r"https?://[^\s<>()\]\"']+", r.get("text") or "")
        sm_checked += len(urls_in)
        children = [u for u in urls_in if u.endswith(".xml")]
        if children and len(children) == len(urls_in):  # sitemap index: one more level
            kids, _ = tf.fetch(children[:5], links=False, image_links=False, page_metadata=False)
            for k in kids.values():
                ku = re.findall(r"https?://[^\s<>()\]\"']+", k.get("text") or "")
                sm_checked += len(ku)
                sm_found = bool(sm_found) or any(norm(u) in keys for u in ku)
            sm_found = bool(sm_found)
        else:
            sm_found = bool(sm_found) or any(norm(u) in keys for u in urls_in)
    if sm_checked == 0:
        sm_found = None  # nothing usable to check against
    elif sm_found is None:
        sm_found = False

    # ---- SEARCH: does the page surface for the query?
    serp, rank, entry = [], None, None
    for pg in range(3):
        batch = tf.search(query, page=pg, **({"location": country} if country else {}))
        serp += batch
        for i, r in enumerate(batch, 1):
            if norm(r["url"]) in keys:
                rank, entry = pg * len(batch) + i, r
                break
        if rank or not batch:
            break
    own, own_entry = [], None
    if rank is None:  # diagnostic: is it indexed at all, if we restrict to its own domain?
        own = tf.search(f"site:{host} {' '.join(tokens(h1 or title)[:8])}")
        own_entry = next((r for r in own if norm(r["url"]) in keys), None)
    seen = entry or own_entry
    if seen:
        seen = {**seen, "title": clean(seen.get("title")), "snippet": tidy_snippet(seen.get("snippet"))}

    # ---- competitors: fetch the pages that outrank us and profile them the same way
    cand = [(i, r) for i, r in enumerate(serp, 1) if norm(r["url"]).split("/")[0] != norm(url).split("/")[0]][:6]
    comps, comp_err, kept = [], [], []
    if cand:
        cg, ce = tf.fetch([r["url"] for _, r in cand], links=False, image_links=False, page_metadata=False)
        for i, r in cand:
            if len(comps) >= 3:
                break
            if r["url"] not in cg:
                comp_err.append((i, r["url"], (ce.get(r["url"]) or {}).get("error")))
                continue
            pr = profile(cg[r["url"]], terms, i)
            if looks_like_challenge(pr["title"], cg[r["url"]].get("text"), pr["words"]):
                comp_err.append((i, r["url"], "bot_challenge_page"))  # not real content: keep out of the table and medians
                continue
            comps.append(pr)
            kept.append(r["url"])
    if comps:
        hg, _ = tf.fetch(kept, format="html", include_selectors=["h1"], links=False, image_links=False, page_metadata=False)
        for c in comps:
            src = next((v for k, v in hg.items() if norm(k) == norm(c["url"]) or norm(v.get("final_url") or "") == norm(c["url"])), None)
            if src:
                o = Outline()
                o.feed(src.get("text") or "")
                c["h1"] = best_h1(o.headings, c["title"], c["h1"])
                c["t_h1"] = term_hits(terms, c["h1"])

    # =========================================================== findings
    # -- access
    if norm(final_url) != norm(url):
        add("access", "low", "URL redirects", f"{url} -> {final_url}", "Link internally and in sitemaps to the final URL.", "search",
            "Redirect hops dilute signals and some AI fetchers do not follow them.")
    if robots_txt:
        for role, sev, why in (("search", "high", "This bot builds the index AI search answers cite."),
                               ("user", "med", "This bot fetches the page when a user asks an AI assistant about it.")):
            bl = [b for b, r in BOTS.items() if r == role and blocked.get(b)]
            if bl:
                add("access", sev, f"robots.txt blocks {role} crawlers for this path", f"Blocked for: {', '.join(bl)} (path {path}).",
                    "Remove or narrow the matching Disallow rule in robots.txt, or add an explicit Allow for these agents.", "both", why)
        tr = [b for b, r in BOTS.items() if r == "training" and blocked.get(b)]
        if tr:
            add("access", "info", "Training crawlers are blocked (policy choice)", f"Blocked: {', '.join(tr)}.",
                "No action needed if intentional. Blocking these does not by itself hide you from AI search citations.", "ai")
    tags = " ".join(dict.fromkeys(" ".join([str(meta.get("robots") or ""), (raw or {}).get("meta", {}).get("robots", ""), (raw or {}).get("x_robots", "")]).lower().split()))
    if "noindex" in tags:
        add("access", "high", "Page is marked noindex", f"Robots directives seen: {tags.strip()}", "Remove noindex (meta robots or X-Robots-Tag header) if you want this page found.", "both",
            "Engines drop noindex pages from results entirely.")
    if re.search(r"nosnippet|max-snippet:\s*0", tags):
        add("access", "med", "Snippet use is forbidden", f"Robots directives seen: {tags.strip()}", "Remove nosnippet / max-snippet:0 so engines and AI answers may quote you.", "both",
            "No snippet permission means no quoted answer text, which lowers click-through even when you rank.")
    if readable:
        can = meta.get("canonical")
        if not can:
            add("metadata", "med", "No canonical URL", "page_metadata has no canonical.", f'Add <link rel="canonical" href="{final_url}"> in <head>.', "search",
                "Without it, duplicate/parameter URLs compete with each other for the same query.")
        elif norm(can) != norm(final_url):
            add("metadata", "high", "Canonical points to a different URL", f"canonical={can}  but fetched page is {final_url}",
                "Point the canonical at this URL, or expect the other URL to be indexed instead of this one.", "search",
                "This page tells engines to rank another page for its content.")
    if sm_found is False:
        add("access", "med", "Page not found in sitemap", f"Checked {sm_checked} URLs across {len(sm_got)} sitemap file(s).", "Add the page to sitemap.xml with an accurate <lastmod>.", "search",
            "Discovery then depends on internal links alone.")
    elif sm_found is None and smaps:
        why = next(iter(sm_err.values()), {}).get("error", "no URLs in the response")
        add("access", "low", "Declared sitemap could not be read", f"robots.txt points to {', '.join(sm_urls[:2])} but Fetch got no usable URL list ({why}).",
            "Open it in a browser to confirm it works. This may be a Fetch limitation, so treat it as unverified.", "search")
    elif sm_found is None:
        add("access", "med", "No sitemap found", f"{sm_urls[0]} returned no URL list (missing, or a single-page app answering with its home page).",
            "Publish sitemap.xml and reference it from robots.txt with a Sitemap: line.", "search", "Discovery then depends on internal links alone.")
    if not llms_txt:
        add("access", "low", "No llms.txt", f"{origin}/llms.txt returned nothing.",
            "Optional: publish llms.txt listing key pages with one-line descriptions. Adoption is uneven and no major engine documents it as a ranking factor.", "ai")

    # -- content (what AI tools can read)
    if readable:
        if me["words"] < 150:
            add("content", "high", f"Only {me['words']} words extracted", "Fetch rendered the page in a real browser and still found almost no text.",
                "Put the substantive answer in HTML text, not images/canvas/widgets.", "both", "Little extractable text means little for an engine to match against a query.")
        if raw:
            cov, miss = coverage(md, raw["text"])
            if cov is not None and cov < 0.85:
                sev = "high" if cov < 0.5 else "med"
                add("content", sev, "Much of the text only exists after JavaScript runs",
                    f"{round((1 - cov) * 100)}% of sampled passages from the rendered page are absent from the raw HTML. Example passage missing from raw HTML: \"{miss[0]}...\"",
                    "Server-side render or statically generate the main content (SSR/SSG). Verify with `curl -s URL | grep -i 'a phrase from the page'`.", "both",
                    "Crawlers that do not execute JavaScript, which reportedly includes several AI crawlers, index and quote only the raw HTML.")
            if not raw["jsonld"]:
                add("content", "med", "No structured data (JSON-LD)", "No application/ld+json block in the raw HTML.",
                    "Add JSON-LD. Starter based on what Fetch extracted:\n" + jsonld_skeleton(me, desc, meta, final_url,
                    (meta.get("og") or {}).get("type") == "article" or bool(re.search(r"/(blog|news|articles?|posts?|guides?)/", path))), "both",
                    "Structured data gives engines and AI tools unambiguous facts (type, author, date) instead of inferring them.")
            else:
                types, bad, gaps = ld_summary(raw["jsonld"])
                if bad:
                    add("content", "med", "Invalid JSON-LD", f"{bad} block(s) fail to parse.", "Validate with a JSON linter and Google's Rich Results Test.", "both")
                for t, m in gaps:
                    add("content", "low", f"{t} JSON-LD is missing properties", f"Missing: {', '.join(m)}.", f"Add {', '.join(m)} to the {t} object.", "both")
        elif raw_err:
            add("content", "info", "Raw HTML check skipped", raw_err, "Re-run with network access to the page.", "ai")
        heads = dom_heads or md_stats(md)[0]
        h1s = [t for l, t in heads if l == 1]
        if not h1s:
            add("content", "med", "No H1 in extracted content", f"No <h1> found {'in the rendered HTML' if dom_heads else 'in the extracted content'}.", "Add one H1 stating the page's main topic in plain words.", "both",
                "The H1 is the strongest on-page label an extractor has for what the page is about.")
        elif len(h1s) > 1:
            add("content", "low", f"{len(h1s)} H1s", "; ".join(h1s[:4]), "Keep one H1; demote the rest to H2.", "ai")
        sk = first_skip(heads)
        if sk:
            (la, ta), (lb, tb) = sk
            add("content", "low", "Heading levels skip", f'H{la} "{ta[:50]}" is followed directly by H{lb} "{tb[:50]}".',
                "Use consecutive heading levels so sections nest cleanly.", "ai")
        real = [i for i in images if not i["decorative"]]
        noalt = [i for i in real if i["alt"] is None]
        if len(real) >= 3 and len(noalt) / len(real) > 0.3:
            add("content", "med", f"{len(noalt)} of {len(real)} content images have no alt attribute",
                "Examples: " + ", ".join(i["src"][:70] for i in noalt[:3]), "Add descriptive alt text (alt=\"\" only for purely decorative images).", "both",
                "AI tools cannot see the image; without alt text its information is lost, and image search cannot place it.")
        if is_article and not (page.get("published_date") and page.get("author")):
            miss = [k for k, v in (("published date", page.get("published_date")), ("author", page.get("author"))) if not v]
            add("content", "med", f"Article exposes no {' or '.join(miss)}", "TinyFish Fetch found none, so an AI tool reading the page will not have it either.",
                "Show byline and date in visible HTML and mirror them in Article JSON-LD (datePublished, dateModified, author).", "both",
                "Freshness and attribution are how AI answers decide whether a page is safe to cite.")

        # -- metadata
        if not title or len(title) < 20 or len(title) > 65:
            site_name = (meta.get("og") or {}).get("site_name", "")
            idea = suggest_title(f"{h1} - {query}" if h1 and me["t_title"] is not None and me["t_title"] < 1 else (h1 or title or query), title, site_name)
            add("metadata", "med", "Title tag length" if title else "Missing title tag", f"Title ({len(title)} chars): {title!r}. Aim for roughly 30-60.",
                "Rewrite it to state the page topic in plain words." + (f" Starting point (edit it): {idea}" if idea and idea != title else ""), "search",
                "The title is the main relevance signal and the clickable headline in results.")
        if not desc:
            add("metadata", "med", "No meta description", f"None found (source: {desc_src}).", "Write a 120-160 character summary containing the primary query terms.", "search",
                "Without it the engine invents a snippet from body text.")
        elif not 70 <= len(desc) <= 160:
            add("metadata", "low", "Meta description length", f"{len(desc)} chars.", "Aim for 120-160 characters.", "search")
        og = meta.get("og") or {}
        miss = [k for k in ("title", "description", "image") if not og.get(k)]
        if miss:
            add("metadata", "low", "Open Graph tags incomplete", f"Missing og:{', og:'.join(miss)}.", "Add the missing og: tags; link previews and many AI tools use them.", "both")
        if not meta.get("viewport"):
            add("metadata", "low", "No viewport meta tag", "", 'Add <meta name="viewport" content="width=device-width, initial-scale=1">.', "search", "Mobile-first indexing.")
        if not page.get("language"):
            add("metadata", "low", "No language declared", "", 'Add <html lang="en"> (or the right code).', "ai")

    # -- visibility (Search) tied to readability (Fetch)
    if rank is None:
        if own_entry:
            add("visibility", "high", f"Indexed, but not in the top {len(serp)} for \"{query}\"",
                f"Found on {host} via site: search as \"{own_entry['title']}\", but absent from the general results.",
                "The page is known to the engine but not competitive for this query. See the comparison table for what the pages above it do.", "search")
        else:
            add("visibility", "high", "Page did not surface even when searching its own domain",
                f"Neither \"{query}\" (top {len(serp)}) nor a site:{host} search returned {norm(final_url)}.",
                "Suspect indexing: check noindex, canonical, robots.txt, sitemap and internal links first; then request indexing in Search Console / Bing Webmaster Tools.", "both",
                "Nothing else matters for visibility until the page is in the index.")
    elif rank > 10:
        add("visibility", "med", f"Ranks #{rank} for \"{query}\"", "Below the first results page.", "See the comparison table below.", "search")
    if seen:
        st, sn = seen.get("title", ""), seen.get("snippet", "")
        if title_rewritten(st, title, h1):
            add("visibility", "med", "Search shows a different title than your <title>", f'Shown: "{st}"   Yours: "{title}"',
                "Engines rewrite titles that are too long, boilerplate-heavy, or that disagree with the H1. Make title and H1 say the same thing in under ~60 characters.", "search",
                "A rewritten title means the engine did not trust your markup to describe the page.")
        src = snippet_source(sn, desc, md)
        if src == "page body" and readable:
            add("visibility", "low", "Snippet is pulled from body text, not your meta description", f"Shown: '{sn[:160]}'",
                "Rewrite the meta description to answer the query directly, using its key terms." if desc else "Add a meta description.", "search")
    if terms and readable:
        cmp_ = lambda k: avg([c[k] for c in comps])
        for key, label, sev in (("t_title", "title tag", "high"), ("t_h1", "H1", "med"), ("t_first", "first 120 words", "med")):
            mine, theirs = me[key], cmp_(key)
            if mine is not None and mine < 1 and theirs is not None and theirs - mine >= 0.34:
                miss = [t for t in terms if not term_hits([t], {"t_title": title, "t_h1": h1, "t_first": " ".join(md.split()[:120])}[key])]
                add("visibility", sev, f"Query terms missing from your {label}", f"Missing: {', '.join(miss)}. Top results cover {round(theirs * 100)}% of the terms there; you cover {round(mine * 100)}%.",
                    f"Work {', '.join(miss)} into the {label} where it reads naturally.", "search",
                    "Extractors weigh title/H1/opening text most when deciding what a page answers.")
        mw = median([c["words"] for c in comps])
        if mw and me["words"] < 0.5 * mw:
            add("visibility", "med", "Much thinner than the pages that outrank you", f"You: {me['words']} words. Median of top results: {mw}.",
                "Cover the sub-questions those pages answer (see their H2s). Length itself is not the goal; coverage is.", "search")
        if comps and sum(1 for c in comps if c["tables"] or c["lists"]) >= 2 and not (me["tables"] or me["lists"]):
            add("visibility", "low", "Competitors use lists/tables; you use neither", f"{sum(1 for c in comps if c['tables'])} of {len(comps)} have tables, {sum(1 for c in comps if c['lists'])} have lists.",
                "Put comparable facts into a table or list; both are easy for AI tools to lift into an answer.", "search")
        dated = sum(1 for r in serp[:5] if r.get("date"))
        if is_article and dated >= 2 and not page.get("published_date"):
            add("visibility", "med", "Top results show dates; your page exposes none", f"{dated} of the top 5 results carry a date in search.", "Expose a visible publish/updated date and mirror it in JSON-LD.", "search")

    # =========================================================== scores (transparent heuristics)
    ai = max(0, 100 - sum(PEN[f.sev] for f in F if f.affects in ("ai", "both")))
    base = 10 if rank is None else 95 if rank <= 3 else max(50, 80 - 3 * (rank - 4)) if rank <= 10 else 40
    vis = max(0, base - sum(PEN[f.sev] for f in F if f.affects == "search") // 2)
    order = {"high": 0, "med": 1, "low": 2, "info": 3}
    F.sort(key=lambda f: order[f.sev])

    fix_first_titles = select_fix_first(F)

    return {
        "url": url, "final_url": final_url, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "query": query, "query_source": query_src, "rank": rank, "serp_entry": seen, "serp_size": len(serp),
        "scores": {"ai_readability": ai, "search_visibility": vis},
        "page": {**me, "title": title, "description": desc, "description_source": desc_src, "canonical": meta.get("canonical"),
                 "robots": tags.strip(), "images": len(images), "images_missing_alt": len([i for i in images if i["alt"] is None and not i["decorative"]]),
                 "links": len(set(page.get("links") or [])), "excerpt": md[:500],
                 "jsonld_types": ld_summary(raw["jsonld"])[0] if raw else None, "raw_status": (raw or {}).get("status")},
        "site": {"robots_txt": bool(robots_txt), "llms_txt": bool(llms_txt), "in_sitemap": sm_found, "blocked_bots": [b for b, v in blocked.items() if v]},
        "competitors": comps, "competitor_errors": comp_err, "findings": [asdict(f) for f in F], "fix_first": fix_first_titles, "calls": tf.calls,
    }


# --------------------------------------------------------------------------- report
def to_markdown(r: dict) -> str:
    if r.get("status") == "not_found":
        f = r["findings"][0]
        return (f"# AI-readability & search audit\n\n**Page:** {r['url']}  \n**Checked live:** {r['checked_at']}\n\n## Verdict\n\n"
                f"**Not auditable: this URL does not exist.** {f['evidence']}\n\n**Fix:** {f['fix']}\n\n"
                "No scores were computed, because there is no page to score.\n")
    P, S = r["page"], r["scores"]
    sm = {None: "unknown"}.get(r["site"]["in_sitemap"], r["site"]["in_sitemap"])
    ld = ", ".join(t if n == 1 else f"{t} (x{n})" for t, n in Counter(P["jsonld_types"] or []).items())
    esc = lambda s: str(s).replace("|", "\\|").replace("\n", " ")
    rank = f"#{r['rank']}" if r["rank"] else f"not in top {r['serp_size']}"
    o = [f"# AI-readability & search audit\n", f"**Page:** {r['final_url']}  \n**Checked live:** {r['checked_at']}  \n"
         f"**Query:** \"{r['query']}\" ({r['query_source']})\n",
         "## Verdict\n", f"- **AI readability:** {S['ai_readability']}/100", f"- **Search visibility for the query:** {rank} (score {S['search_visibility']}/100)",
         f"- **Findings:** {sum(f['sev'] == 'high' for f in r['findings'])} high, {sum(f['sev'] == 'med' for f in r['findings'])} medium, "
         f"{sum(f['sev'] == 'low' for f in r['findings'])} low\n",
         "_Scores are transparent heuristics: readability = 100 minus finding penalties (high 15, medium 7, low 3); visibility starts from rank and is reduced by search-side findings._\n",
         "## Fix first\n"]

    # Fix First Section logic
    ff = r.get("fix_first", [])
    if not ff:
        o.append("Nothing urgent. See the full list below.\n")
    else:
        # We need the actual Finding objects to get the fix text
        # The audit results contain findings as dicts
        findings_map = {f['title']: f for f in r['findings']}
        for title in ff:
            f = findings_map.get(title)
            if not f: continue
            # Shorten fix to one sentence
            fix_text = f['fix'].split('. ')[0] + '.' if '.' in f['fix'] else f['fix'].splitlines()[0]
            o.append(f"- **{title}**: {fix_text}\n")
        o.append("\n")

    o += ["## What an AI tool extracts (TinyFish Fetch)\n",
         f"| Signal | Value |\n|---|---|\n| Words extracted | {P['words']} |\n| Title | {esc(P['title'])} |\n| H1 | {esc(P['h1'] or 'none')} |\n"
         f"| H2 / tables / lists | {P['h2']} / {P['tables']} / {P['lists']} |\n| Meta description ({P['description_source']}) | {esc(P['description'] or 'none')} |\n"
         f"| Canonical | {esc(P['canonical'] or 'none')} |\n| Images (missing alt) | {P['images']} ({P['images_missing_alt']}) |\n| Unique links | {P['links']} |\n"
         f"| JSON-LD types | {esc(ld or 'none / not checked')} |\n"
         f"| Published / author | {esc(P['published'] or 'none')} / {esc(P['author'] or 'none')} |\n"
         f"| robots.txt / llms.txt / in sitemap | {r['site']['robots_txt']} / {r['site']['llms_txt']} / {sm} |\n"
         f"| Bots blocked by robots.txt | {esc(', '.join(r['site']['blocked_bots']) or 'none')} |\n",
         "First 500 characters as extracted:\n\n```\n" + P["excerpt"] + "\n```\n",
         "## How it shows up (TinyFish Search)\n"]
    e = r["serp_entry"]
    o.append(f"- Position for the query: **{rank}**")
    if e:
        o += [f"- Search shows the title: {esc(e.get('title'))}", f"- Search shows the snippet: {esc(e.get('snippet'))}"]
    if r["competitors"]:
        rows = ["| | You | " + " | ".join(f"#{c['rank']} {urlparse(c['url']).netloc}" for c in r["competitors"]) + " |", "|---|---|" + "---|" * len(r["competitors"])]
        pc = lambda v: "n/a" if v is None else f"{round(v * 100)}%"
        for label, key, fmt in (("Query terms in title", "t_title", pc), ("Query terms in H1", "t_h1", pc), ("Query terms in first 120 words", "t_first", pc),
                                ("Words", "words", str), ("H2 sections", "h2", str), ("Tables", "tables", str), ("Lists", "lists", str),
                                ("Question-style headings", "questions", str), ("Published date exposed", "published", lambda v: "yes" if v else "no")):
            rows.append(f"| {label} | {fmt(P[key])} | " + " | ".join(fmt(c[key]) for c in r["competitors"]) + " |")
        o += ["", "### Readability vs. the pages that outrank you (all read with the same Fetch call)\n"] + rows
    for i, u, err in r["competitor_errors"]:
        o.append(f"\n_Result #{i} ({u}) could not be fetched: {err}._")
    o.append("\n## Findings\n")
    for f in r["findings"]:
        o.append(f"### [{f['sev'].upper()}] {f['title']}  \n_{f['area']} · affects {f['affects']}_\n")
        o.append(f"- **Evidence:** {f['evidence']}")
        o.append(f"- **Fix:** {f['fix'].splitlines()[0]}")
        if "\n" in f["fix"]:
            o.append("\n```json\n" + "\n".join(f["fix"].splitlines()[1:]) + "\n```")
        if f["ties"]:
            o.append(f"- **Why it hurts visibility:** {f['ties']}")
        o.append("")
    o.append("## How this audit was produced\n")
    o.append("Every request below went to the live page or live search index (Fetch used `ttl=0`).\n")
    for kind, what in r["calls"]:
        o.append(f"- TinyFish {kind}: `{what[:110]}`")
    o.append("- Plain HTTP GET (not TinyFish): raw HTML for JSON-LD, meta description and the JavaScript-dependence check.")
    return "\n".join(o) + "\n"


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env reader (KEY=value lines). Real environment variables win over the file."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip().removeprefix("export ").strip(), v.strip().strip("\"'"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("-q", "--query", help="target search query (derived from the page if omitted)")
    ap.add_argument("-o", "--out", default="report", help="output prefix -> <out>.md and <out>.json")
    ap.add_argument("--country", help="country code for geo-targeted search, e.g. IN, US")
    ap.add_argument("--no-raw", action="store_true", help="skip the plain HTTP GET (no JSON-LD / JS-dependence checks)")
    a = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    load_dotenv()
    key = os.environ.get("TINYFISH_API_KEY")
    if not key:
        sys.exit("Set TINYFISH_API_KEY (free at agent.tinyfish.ai/api-keys).")
    url = a.url if re.match(r"https?://", a.url) else "https://" + a.url
    try:
        res = audit(url, a.query, TinyFish(key), do_raw=not a.no_raw, country=a.country)
    except requests.HTTPError as ex:
        sys.exit(f"TinyFish API error {ex.response.status_code}: {ex.response.text[:300]}")
    with open(a.out + ".md", "w", encoding="utf-8") as f:
        f.write(to_markdown(res))
    with open(a.out + ".json", "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2, default=str)
    if res.get("status") == "not_found":
        print(f"NOT AUDITABLE: {res['findings'][0]['title']} - {res['url']}")
        print(f"wrote {a.out}.md and {a.out}.json")
        return
    s = res["scores"]
    print(f"AI readability {s['ai_readability']}/100 | visibility {s['search_visibility']}/100 | rank {res['rank'] or 'not found'} for \"{res['query']}\"")
    for f in res["findings"][:5]:
        print(f"  [{f['sev']}] {f['title']}")
    print(f"wrote {a.out}.md and {a.out}.json")


if __name__ == "__main__":
    main()
