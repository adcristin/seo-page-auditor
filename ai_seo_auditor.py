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
import random
import re
import sys
import html
import time
import ipaddress
from dataclasses import dataclass, asdict
from html.parser import HTMLParser
from urllib.parse import urlparse, parse_qsl, urlencode

import requests
import socket

SEARCH_URL = "https://api.search.tinyfish.ai"
FETCH_URL = "https://api.fetch.tinyfish.ai"
UA = "Mozilla/5.0 (compatible; ai-seo-auditor/0.1)"

RETRY_MAX_ATTEMPTS = 3
RETRY_BASE_DELAY = 2.0
RETRY_JITTER_RANGE = (0.1, 0.5)

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

def e(text) -> str:
    """Escape untrusted text for HTML interpolation. Every page value goes through this."""
    return html.escape(str(text))

# --------------------------------------------------------------------------- redaction
API_KEY_VALUE = os.environ.get("TINYFISH_API_KEY", "")

KEY_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"X-API-Key[:\s=]+[a-zA-Z0-9_\-]+", re.I),
]


def redact(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    # Remove configured API key value
    if API_KEY_VALUE:
        text = text.replace(API_KEY_VALUE, "[REDACTED_KEY]")
    # Remove patterns that look like secrets
    for pat in KEY_PATTERNS:
        text = pat.sub("[REDACTED_KEY]", text)
    return text


def redact_value(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    return value


# --------------------------------------------------------------------------- TinyFish client
class TinyFish:
    """Thin REST wrapper. Logs every call so the report can show its own provenance."""

    def __init__(self, api_key: str, timeout: int = 30, max_retries: int = RETRY_MAX_ATTEMPTS):
        self.s = requests.Session()
        self.s.headers["X-API-Key"] = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.calls: list[tuple[str, str]] = []

    def _get_retry_after(self, r: requests.Response) -> float | None:
        after = r.headers.get("Retry-After")
        if not after:
            return None
        try:
            return float(after)
        except ValueError:
            # It could be an HTTP date; for simplicity in this CLI, we skip date parsing
            # and fall back to exponential backoff.
            return None

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        last_ex = None
        for attempt in range(1, self.max_retries + 1):
            try:
                r = self.s.request(method, url, **kwargs)
                if r.status_code == 429 or 500 <= r.status_code < 600:
                    if attempt == self.max_retries:
                        r.raise_for_status()

                    delay = self._get_retry_after(r) or (RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                    delay += random.uniform(*RETRY_JITTER_RANGE)
                    time.sleep(delay)
                    continue

                r.raise_for_status()
                return r
            except (requests.Timeout, requests.ConnectionError) as ex:
                last_ex = ex
                if attempt == self.max_retries:
                    raise
                delay = (RETRY_BASE_DELAY * (2 ** (attempt - 1))) + random.uniform(*RETRY_JITTER_RANGE)
                time.sleep(delay)
        raise last_ex

    def search(self, query: str, **params) -> list[dict]:
        r = self._request_with_retry("GET", SEARCH_URL, params={"query": query, **params}, timeout=self.timeout)
        self.calls.append(("search", f"{query} (page {params.get('page', 0)})"))
        return r.json().get("results", [])

    def fetch(self, urls: list[str], **body) -> tuple[dict, dict]:
        payload = {"urls": urls, "format": "markdown", "ttl": 0, **body}  # ttl=0 -> live, not cached
        r = self._request_with_retry("POST", FETCH_URL, json=payload, timeout=self.timeout)
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
    confidence: str = ""  # verified | likely | unverified
    verify: str = ""      # one-line command or step to check it
    snippet: str = ""     # ready-to-paste fix built only from page facts
    agent_prompt: str = ""  # short coding-assistant instruction


def norm(u: str) -> str:
    """Compare URLs ignoring scheme, www, trailing slash and tracking params."""
    p = urlparse(u.strip())
    host = p.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    q = urlencode([(k, v) for k, v in parse_qsl(p.query) if not re.match(r"(utm_|fbclid|gclid|mc_)", k)])
    return host + (p.path.rstrip("/") or "") + (("?" + q) if q else "")


def norm_query(s: str) -> str:
    return " ".join((s or "").lower().split())


def normalize_target(raw: str) -> tuple[str, str | None]:
    """Normalize input URL and validate public web access.

    Returns (url, note) where note is "Interpreted as {url}" if input changed, else None.
    Raises ValueError for invalid URLs, unsupported schemes, or non-public hosts.
    """
    if not raw or not isinstance(raw, str):
        raise ValueError("That doesn't look like a URL")

    # 1. Cleaning: strip whitespace, surrounding quotes, and < >
    s = raw.strip()
    while True:
        cleaned = s.strip(" \t\r\n'\"<>")
        if cleaned == s:
            break
        s = cleaned
    if not s:
        raise ValueError("That doesn't look like a URL")

    # Remove #fragments
    s = s.split("#", 1)[0]

    # 2. Scheme handling
    if s.startswith("//"):
        s = "https:" + s
    elif "://" in s:
        scheme_part, rest = s.split("://", 1)
        scheme_part = scheme_part.lower()
        if scheme_part not in ("http", "https"):
            raise ValueError(f"Unsupported scheme '{scheme_part}'. TinyFish only supports http and https URLs.")
        s = f"{scheme_part}://{rest}"
    elif ":" in s:
        prefix, rest = s.split(":", 1)
        port_candidate = rest.split("/")[0]
        if port_candidate.isdigit():
            s = "https://" + s
        else:
            scheme_part = prefix.lower()
            if scheme_part not in ("http", "https"):
                raise ValueError(f"Unsupported scheme '{scheme_part}'. TinyFish only supports http and https URLs.")
            s = f"{scheme_part}://{rest}"
    else:
        s = "https://" + s

    p = urlparse(s)
    scheme = p.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"Unsupported scheme '{scheme}'. TinyFish only supports http and https URLs.")

    # 3. Host validation
    host_str = p.hostname
    if not host_str:
        raise ValueError("That doesn't look like a URL")

    # Reject embedded credentials
    if p.username or p.password:
        raise ValueError("URLs with embedded credentials (user:pass@) are not allowed.")

    # Reject query strings that look like they carry secrets
    query_lower = p.query.lower()
    secret_query_keys = ["token", "key", "signature", "password"]
    for k, v in parse_qsl(p.query):
        if k.lower() in secret_query_keys:
            raise ValueError(f"Query parameter '{k}' suggests a secret; URLs with query secrets are not allowed.")

    # Check localhost
    if host_str.lower() == "localhost":
        raise ValueError("TinyFish requires public web URLs; 'localhost' is not accessible.")

    # Check IP address
    try:
        ip = ipaddress.ip_address(host_str)
        is_ip = True
    except ValueError:
        is_ip = False
        ip = None

    if is_ip:
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError(f"TinyFish requires public web URLs; '{host_str}' is a private or local address.")
    else:
        # Host must contain a dot
        if "." not in host_str or host_str.startswith(".") or host_str.endswith("."):
            raise ValueError("That doesn't look like a URL")

    # 4. Punycode (IDNA)
    try:
        ascii_host = host_str.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("That doesn't look like a URL")

    # 5. Reconstruct normalized URL (no userinfo allowed)
    netloc_parts = [ascii_host]
    if p.port:
        netloc_parts.append(f":{p.port}")
    new_netloc = "".join(netloc_parts)

    path = p.path or ""
    query = f"?{p.query}" if p.query else ""
    normalized_url = f"{scheme}://{new_netloc}{path}{query}"

    note = f"Interpreted as {normalized_url}" if normalized_url != raw else None
    return normalized_url, note


def tokens(s: str) -> list[str]:
    return re.findall(r"\w+", (s or "").casefold(), re.UNICODE)


def best_passage(md: str, terms: list[str]):
    """Split markdown into passages and return the best for query terms."""
    # Exclude boilerplate/hatnotes first
    BOILERPLATE_PHRASES = {"redirects here", "for other uses", "from wikipedia", "this article", "jump to", "cookie", "consent", "navigation", "table of contents"}
    cleaned = re.sub(r"```.*?```", "", md, flags=re.DOTALL)
    cleaned = re.sub(r"`[^`]*`", "", cleaned)
    lines = []
    for l in cleaned.splitlines():
        s = l.strip()
        if not s:
            continue
        if re.match(r"^\s*[-*]\s*\[.*?\]\(.*?\)\s*$", s):
            continue
        if re.match(r"^\s*(Table of Contents|TOC|Navigation|Menu)\s*$", s, re.I):
            continue
        lines.append(s)
    # Build passages without crossing heading boundaries
    passages = []
    current_words = []
    def flush():
        nonlocal current_words
        text = " ".join(current_words).strip()
        if text:
            passages.append(text)
        current_words = []
    for l in lines:
        # Heading boundary: flush current and start new if heading line
        if re.match(r"^#{1,6}\s+", l):
            flush()
            # Skip the heading line itself from passage (don't include heading text as passage start)
            continue
        current_words.append(l)
        wcount = len(re.findall(r"\w+", " ".join(current_words)))
        if wcount >= 25:
            if wcount > 120:
                all_words = " ".join(current_words).split()
                chunk_words = all_words[:120]
                passages.append(" ".join(chunk_words))
                rest = all_words[120:]
                current_words = [" ".join(rest)]
            else:
                flush()
    if current_words:
        text = " ".join(current_words).strip()
        if text:
            passages.append(text)
    # Filter passages: exclude boilerplate/hatnote lines
    filtered = []
    for p in passages:
        lower_p = p.lower()
        # Skip short nav-like lines
        if len(p.split()) < 6:
            continue
        # Skip passages containing boilerplate phrases
        has_boilerplate = any(bp in lower_p for bp in BOILERPLATE_PHRASES)
        if has_boilerplate:
            continue
        filtered.append(p)
    # Prefer 25-120 word sentence-like passages (contain period or question mark)
    result = []
    for p in filtered:
        pw = len(re.findall(r"\w+", p))
        if 25 <= pw <= 120:
            if ("." in p or "?" in p):
                result.append(p)
    # If no sentence-like passages found, fall back to any 25-120 word passage
    if not result:
        for p in filtered:
            pw = len(re.findall(r"\w+", p))
            if 25 <= pw <= 120:
                result.append(p)
    if not terms or not result:
        return None
    best = None
    best_score = -1
    best_words = None
    best_offset = None
    for idx, p in enumerate(result):
        text_lower = p.lower()
        hits = 0
        for t in terms:
            # Token match with acronym
            t_lower = t.lower()
            matched = False
            if t_lower in text_lower.split():
                # Actually need token-level check; use tokens
                pass
            # Use token-based matching
            p_tokens = set(tokens(p.lower()))
            x_lower = t_lower
            if x_lower in p_tokens:
                matched = True
            else:
                plural_x = x_lower + "s"
                possessive_x = x_lower + "'s"
                for tok in p_tokens:
                    if tok == plural_x or tok == possessive_x:
                        matched = True
                        break
            if matched:
                hits += 1
            else:
                initials = "".join(w[0] for w in re.findall(r"\w+", p) if len(w) > 1)
                if x_lower in initials.lower():
                    hits += 1
        sentence_bonus = 0.05 if ("." in p or "?" in p) else 0
        coverage = (hits / len(terms)) + sentence_bonus if terms else 0
        # Cap coverage at 100%
        coverage = min(coverage, 1.0)
        if coverage > best_score:
            best_score = coverage
            best = p
            best_words = len(re.findall(r"\w+", p))
            best_offset = len(re.findall(r"\w+", " ".join(result[:idx])))
    return {
        "passage": best,
        "coverage": round(best_score, 2),
        "offset_words": best_offset,
        "length_words": best_words,
    }

def normalize_text_for_parity(text: str) -> str:
    """Strip artifacts that don't exist in raw HTML text: list numbers/bullets, footnote markers, pipes, emphasis, backticks, code fences."""
    text = text or ""
    lines = text.splitlines()
    result_lines = []
    skip_sections = False
    skip_headers = {"references", "notes", "citations", "external links", "see also", "further reading"}
    for line in lines:
        stripped = line.strip()
        lower = stripped.lower()
        if lower in skip_headers or lower.startswith(tuple(skip_headers)):
            skip_sections = True
            continue
        if skip_sections and re.match(r"^#{1,6}\s+", line):
            skip_sections = False
        if skip_sections:
            if re.match(r"^#{1,6}\s+", line):
                skip_sections = False
            else:
                continue
        line = re.sub(r"^\s*(?:\d+[.)]\s+|[-*+]\s+)", "", line)
        line = re.sub(r"\[\d+\]", "", line)
        line = re.sub(r"\|", " ", line)
        line = re.sub(r"[*_`]+", "", line)
        line = re.sub(r"```.*?```", " ", line, flags=re.DOTALL)
        line = re.sub(r"`[^`]*`", " ", line)
        result_lines.append(line)
    return " ".join(result_lines)


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


def decode_body_bytes(content: bytes, content_type: str = "") -> str:
    """Decodes raw bytes using the same logic as decode_body, but for pre-fetched bytes."""
    if re.search(r"charset=", content_type, re.I):
        # Requests' .text uses r.encoding. Since we don't have the response object here,
        # we'll try utf-8 first, then fallback.
        pass
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("cp1252", errors="replace")


def is_cjk(s: str) -> bool:
    """Heuristic: check if there's a significant presence of CJK characters."""
    if not s:
        return False
    cjk_count = sum(1 for c in s if '一' <= c <= '鿿' or '぀' <= c <= 'ヿ' or '가' <= c <= '힯')
    return cjk_count / len(s) > 0.1

def get_terms(s: str, use_bigrams: bool = False) -> set[str]:
    """Returns a set of terms. If use_bigrams is True (for CJK), returns character bigrams."""
    if not s:
        return set()
    if not use_bigrams:
        return set(tokens(s))
    # CJK bigram fallback
    text = s.casefold()
    return {text[i:i+2] for i in range(len(text) - 1)}

def term_hits(terms: list[str], text: str):
    """Share of query terms present in text. 'seo' also matches 'Search Engine Optimization'. Uses token matching (not substring) with simple plural/possessive 's' allowed."""
    if not terms:
        return None
    t = clean(text).lower()
    cjk = is_cjk(t)
    search_text = t if not cjk else t.casefold()
    hits = 0
    text_tokens = set(tokens(search_text))
    for x in terms:
        x_lower = x.lower()
        if cjk:
            # For CJK, keep substring fallback but only for bigram terms
            if x_lower in search_text:
                hits += 1
            continue
        # Token matching with simple plural/possessive "s"
        matched = False
        if x_lower in text_tokens:
            matched = True
        else:
            # Allow plural/possessive "s"
            plural_x = x_lower + "s"
            possessive_x = x_lower + "'s"
            for tok in text_tokens:
                if tok == plural_x or tok == possessive_x:
                    matched = True
                    break
        if matched:
            hits += 1
        elif 3 <= len(x_lower) <= 6:
            # Acronym logic: 'seo' matches 'S... E... O...'
            initials = "".join(w[0] for w in tokens(t) if len(w) > 1)
            if x_lower in initials.lower():
                hits += 1
    return hits / len(terms)


def key_terms(q: str) -> list[str]:
    t = tokens(q)
    # Only apply English stopword removal if the tokens look like English (ASCII)
    if t and all(ord(c) < 128 for tok in t for c in tok):
        return [tok for tok in t if tok not in STOP]
    return t


def extract_topic_gaps(audited_page, comps_data, query_terms):
    """Find topics present in competitors but missing from the audited page."""
    BOILERPLATE = {"navigation", "table of contents", "related", "comments", "share", "menu", "search", "footer", "view post in", "top posts", "subscribe", "frequently asked questions", "introduction", "customer testimonials", "integrations"}
    IMPERATIVE_CTA_STARTS = {"get", "try", "book", "start", "sign up", "generate", "request", "download", "contact", "revolutionize", "join", "subscribe", "learn more"}
    # Brand token for competitor filtering
    audit_md = audited_page.get("text") or ""
    cjk = is_cjk(audit_md)
    # Get competitor brand tokens from URLs
    comp_brand_tokens = set()
    for c in comps_data:
        url = c.get("url", "")
        host = urlparse(url).netloc.lower() if url else ""
        token = host.split(".")[0] if host else ""
        if token and len(token) >= 4:
            comp_brand_tokens.add(token)

    # 1. Extract and normalize competitor H2s
    comp_topics = []
    for idx, c in enumerate(comps_data):
        h2s = c.get("h2_texts", [])
        for h in h2s:
            h_clean = clean(h)
            # Strip numbering prefixes
            h_clean = re.sub(r"^\s*\d+[.)]\s*", "", h_clean)
            h_clean = re.sub(r"^\s*\d+\)\s*", "", h_clean)
            # Drop boilerplate headings
            lower_h = h_clean.lower()
            if any(b in lower_h for b in BOILERPLATE):
                continue
            # Drop imperative CTAs
            first_word = lower_h.split()[0] if lower_h.split() else ""
            if first_word in IMPERATIVE_CTA_STARTS:
                continue
            # Drop headings that contain a competitor's own brand token (only whole-word match for token >=4)
            has_comp_brand = False
            for bt in comp_brand_tokens:
                words = set(tokens(h_clean.lower()))
                if bt.lower() in words:
                    has_comp_brand = True
                    break
            if has_comp_brand:
                continue
            # Drop headings that are only a product/vendor name (single token and matches brand token)
            h_words = tokens(h_clean)
            if len(h_words) <= 2 and any(w.lower() in comp_brand_tokens for w in h_words):
                # This is a competitor name heading; skip unless it also covers general topic
                # Keep if it has 3+ words (more descriptive)
                if len(h_words) < 3:
                    continue
            t_set = get_terms(h_clean, use_bigrams=cjk) - (set(STOP) if not cjk else set())
            if t_set:
                comp_topics.append((h_clean, t_set, idx))

    if not comp_topics:
        return []

    # 2. Merge near-duplicates and identify candidate topics
    topics = []
    for text, t_set, idx in comp_topics:
        merged = False
        for topic in topics:
            overlap = len(t_set & topic["tokens"]) / min(len(t_set), len(topic["tokens"])) if t_set and topic["tokens"] else 0
            if overlap >= 0.6:
                topic["comps"].add(idx)
                merged = True
                break
        if not merged:
            topics.append({"representative": text, "tokens": t_set, "comps": {idx}})

    # 3. Filter by frequency (at least 2 competitors) — removed "contains query term" shortcut
    qualified = []
    for t in topics:
        is_common = len(t["comps"]) >= 2
        if is_common:
            qualified.append(t)

    # If fewer than 2 competitors have headings, or no topic survives, emit no finding
    if len(comps_data) < 2:
        return []
    if not qualified:
        return []

    # 4. Remove topics covered by audited page
    audit_excerpt = " ".join(tokens(audit_md[:2000]))
    audit_heads = " ".join([t for l, t in audited_page.get("dom_heads", []) if l in (2, 3)])
    audit_coverage_text = (audit_excerpt + " " + audit_heads).lower()

    gaps = []
    for t in qualified:
        hits = sum(1 for tok in t["tokens"] if tok in audit_coverage_text)
        if hits < len(t["tokens"]) * 0.7:
            gaps.append(t)

    return [g["representative"] for g in gaps[:6]]



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


# Keep nav/footer text; skip script/style/noscript/template/svg
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


def _is_safe_host(hostname: str) -> bool:
    try:
        info = socket.getaddrinfo(hostname, None)
        for _, _, _, _, sockaddr in info:
            ip_str = sockaddr[0]
            ip = ipaddress.ip_address(ip_str)
            if ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
            if ip.is_private:
                return False
    except Exception:
        pass
    return True


CLOUD_METADATA_IPS = {
    "169.254.169.254", "fe80::a9fe:a9fe",
}


def raw_view(url: str) -> dict:
    """Plain HTTP GET. Not a TinyFish call: Fetch renders JS and hides <script> tags, so this is
    the only way to see JSON-LD and to measure how much text depends on JavaScript."""
    # Resolve and validate host before connecting (SSRF protection)
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Invalid URL for raw GET: no hostname.")
    # Resolve hostname
    try:
        info = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve hostname '{hostname}': {exc}")
    # Refuse private, loopback, link-local, reserved, multicast, cloud metadata
    for _, _, _, _, sockaddr in info:
        ip_str = sockaddr[0]
        ip = ipaddress.ip_address(ip_str)
        if ip_str in CLOUD_METADATA_IPS or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_private:
            raise ValueError(f"Host '{hostname}' resolves to disallowed IP '{ip_str}'.")

    max_hops = 5
    total_start = time.time()
    # Follow redirects manually (max 5 hops) and apply same IP check to each hop
    current_url = url
    for hop in range(max_hops + 1):
        if time.time() - total_start > 25:
            raise ValueError("Raw GET exceeded total time limit (25s).")
        r = requests.get(current_url, headers={"User-Agent": UA}, timeout=25, stream=True, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location")
            if not loc:
                raise ValueError("Redirect response missing Location header.")
            # Resolve redirect target before following
            redirect_parsed = urlparse(loc)
            redirect_host = redirect_parsed.hostname or urlparse(current_url).hostname
            try:
                redirect_info = socket.getaddrinfo(redirect_host, None)
            except socket.gaierror as exc:
                raise ValueError(f"Cannot resolve redirect host '{redirect_host}': {exc}")
            for _, _, _, _, sockaddr in redirect_info:
                redirect_ip_str = sockaddr[0]
                redirect_ip = ipaddress.ip_address(redirect_ip_str)
                if redirect_ip_str in CLOUD_METADATA_IPS or redirect_ip.is_loopback or redirect_ip.is_link_local or redirect_ip.is_reserved or redirect_ip.is_multicast or redirect_ip.is_private:
                    raise ValueError(f"Redirect to disallowed IP '{redirect_ip_str}' refused.")
            # Resolve relative URLs
            current_url = requests.compat.urljoin(current_url, loc)
            continue
        # Final response (not redirect)
        break
    else:
        raise ValueError("Too many redirects (max 5) during raw GET.")

    content_type = r.headers.get("Content-Type", "").lower()
    is_html = "text/html" in content_type or "application/xhtml+xml" in content_type

    # Cap body read to 3MB
    MAX_BODY = 3 * 1024 * 1024
    body_bytes = b""
    truncated = False
    for chunk in r.iter_content(chunk_size=8192):
        body_bytes += chunk
        if len(body_bytes) >= MAX_BODY:
            body_bytes = body_bytes[:MAX_BODY]
            truncated = True
            break

    if not is_html:
        return {
            "status": r.status_code,
            "x_robots": r.headers.get("X-Robots-Tag", ""),
            "non_html": True,
            "content_type": content_type,
            "size": len(body_bytes),
            "text": ""
        }

    p = RawPage()
    p.feed(decode_body_bytes(body_bytes))

    return {
        "status": r.status_code,
        "x_robots": r.headers.get("X-Robots-Tag", ""),
        "title": p.title.strip(),
        "meta": p.meta,
        "jsonld": p.jsonld,
        "text": " ".join(p.text),
        "non_html": False,
        "truncated": truncated
    }


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
    norm_md = normalize_text_for_parity(rendered_md)
    norm_raw = normalize_text_for_parity(raw_text)
    toks = tokens(norm_md)
    if len(toks) < w * 3:
        return None, []
    # Filter windows: must have at least 6 plain words, no digits? The instruction says windows of 6+ plain words without digits.
    # We'll select windows of w words that contain at least 6 words and no digits.
    step = max(1, (len(toks) - w) // n)
    raw_wins = [toks[i:i + w] for i in range(0, len(toks) - w, step)][:n]
    # Filter windows to those with at least 6 words and no digits
    filtered_wins = []
    for win in raw_wins:
        joined = " ".join(win)
        words_only = re.findall(r"[a-zA-Z]+", joined)
        if len(words_only) >= 6 and not re.search(r"\d", joined):
            filtered_wins.append(win)
    if not filtered_wins:
        # If no windows meet criteria, fall back to first windows but with lower quality
        filtered_wins = raw_wins[:n]
    wins = filtered_wins[:n]
    hay = "".join(tokens(norm_raw))  # spacing-insensitive on both sides
    missing = [" ".join(x) for x in wins if "".join(x) not in hay]
    if not wins:
        return None, []
    cov = 1 - len(missing) / len(wins)
    return cov, missing


def shingle_words(text: str, n=5) -> set[str]:
    toks = tokens(text)
    return set(" ".join(toks[i:i+n]) for i in range(len(toks)-n+1)) if len(toks) >= n else set()


def extraction_retention(raw_text: str, md_text: str) -> float | None:
    if not raw_text or not md_text:
        return None
    raw_shingles = shingle_words(raw_text, 5)
    if not raw_shingles:
        return None
    md_shingles = shingle_words(md_text, 5)
    common = raw_shingles & md_shingles
    return len(common) / len(raw_shingles)


BOILERPLATE_HEADINGS = {"contents", "navigation menu", "menu", "table of contents", "personal tools", "namespaces", "views", "tools", "search", "footer"}

def headings_lost(dom_heads: list[tuple[int, str]], md_text: str) -> list[str]:
    md_heads = [t for l, t in md_stats(md_text)[0] if l in (1, 2, 3)]
    md_set = {clean(h) for h in md_heads}
    lost = []
    for lvl, t in dom_heads:
        clean_t = clean(t)
        lower_t = clean_t.lower()
        if lower_t in BOILERPLATE_HEADINGS or lower_t.startswith(tuple(BOILERPLATE_HEADINGS)):
            continue
        if lvl in (1, 2, 3) and clean_t not in md_set:
            lost.append(clean_t)
    # dedupe preserving order
    seen = set()
    unique_lost = []
    for h in lost:
        if h not in seen:
            seen.add(h)
            unique_lost.append(h)
    return unique_lost[:5]


# --------------------------------------------------------------------------- search-side helpers
def snippet_source(snip: str, desc: str, body: str) -> str:
    st = set(tokens(snip)) - STOP
    if not st:
        return "unknown"
    ov = lambda t: len(st & set(tokens(t))) / len(st)
    if desc and ov(desc) >= 0.7:
        return "meta description"
    return "page body" if ov(body) >= 0.7 else "unknown"


def sim(s1: str, s2: str) -> float:
    """Token-set overlap: intersection / min(len)."""
    t1, t2 = set(tokens(s1)), set(tokens(s2))
    return len(t1 & t2) / min(len(t1), len(t2)) if t1 and t2 else 0.0


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


def _max_backtick_run(s: str) -> int:
    max_run = 0
    curr = 0
    for ch in s:
        if ch == '`':
            curr += 1
            max_run = max(max_run, curr)
        else:
            curr = 0
    return max_run

def _fence(s: str) -> str:
    ticks = '`' * (_max_backtick_run(s) + 1)
    if len(ticks) < 3:
        ticks = '```'
    return ticks

def build_snippet_title_meta(h1: str, title: str, query: str, terms: list[str], desc: str, final_url: str, meta: dict, md_text: str) -> str:
    core_title = (h1 or title or "").strip()
    proposal_title = core_title[:55].rsplit(" ", 1)[0] if len(core_title) > 60 else core_title
    if not proposal_title:
        proposal_title = "Page topic"
    proposal_title = proposal_title[:60]
    sentences = re.split(r"(?<=[.!?])\s+", (md_text or "").replace("\n", " ").strip())
    first_sentences = []
    length = 0
    for s in sentences:
        clean_s = clean(s)
        if clean_s and len(clean_s.split()) >= 3:
            first_sentences.append(clean_s)
            length += len(clean_s)
            if length >= 140:
                break
    desc_text = " ".join(first_sentences)[:160]
    page_text = (h1 or "") + " " + (md_text or "") + " " + desc_text
    has_terms = True
    if terms:
        hits = sum(1 for t in terms if t in page_text.lower())
        has_terms = hits > 0
    snippet = f"Proposed title ({len(proposal_title)} chars): {proposal_title}\n"
    snippet += f"Proposed meta description ({len(desc_text)} chars): {desc_text}\n"
    snippet += f"Target URL: {final_url}\n"
    snippet += "Assembled from page H1 and opening sentences only."
    return snippet

def build_snippet_canonical(final_url: str, meta: dict) -> str:
    can = meta.get("canonical", "")
    if can:
        return f'<link rel="canonical" href="{can}">'
    return f'<link rel="canonical" href="{final_url}">'

def build_snippet_og_twitter(title: str, desc: str, meta: dict, final_url: str, first_image_url: str) -> str:
    og = meta.get("og") or {}
    parts = []
    if title:
        parts.append(f'<meta property="og:title" content="{title}" />')
    if desc:
        parts.append(f'<meta property="og:description" content="{desc}" />')
    if first_image_url:
        parts.append(f'<meta property="og:image" content="{first_image_url}" />')
        parts.append(f'<meta property="og:url" content="{final_url}" />')
    twitter = meta.get("twitter") or {}
    if twitter.get("card"):
        card = twitter.get("card", "")
        parts.append(f'<meta name="twitter:card" content="{card}" />')
    if title:
        parts.append(f'<meta name="twitter:title" content="{title}" />')
    if desc:
        parts.append(f'<meta name="twitter:description" content="{desc}" />')
    if first_image_url:
        parts.append(f'<meta name="twitter:image" content="{first_image_url}" />')
    if not parts:
        return "# No real OG/Twitter values found on the page; omit tags with no value."
    return "\n".join(parts)

def build_snippet_jsonld(p: dict, desc: str, meta: dict, final_url: str, is_article: bool, raw_jsonld_blocks: list, path: str) -> str:
    types_found = []
    for b in raw_jsonld_blocks:
        try:
            data = json.loads(b)
            def walk(o):
                if isinstance(o, dict):
                    t = o.get("@type")
                    if t:
                        types_found.append(t)
                    for v in o.values():
                        if isinstance(v, (dict, list)):
                            walk(v)
                elif isinstance(o, list):
                    for item in o:
                        walk(item)
            walk(data)
        except Exception:
            pass
    block_text = " ".join(str(x) for x in (raw_jsonld_blocks or [])).lower()
    has_date_author = bool(p.get("published") or p.get("author"))
    faq_evidence = False
    if raw_jsonld_blocks:
        for b in raw_jsonld_blocks:
            try:
                data = json.loads(b)
                def has_faq(o):
                    if isinstance(o, dict):
                        if o.get("@type") == "FAQPage" or "mainEntity" in o:
                            return True
                        for k, v in o.items():
                            if isinstance(v, (dict, list)):
                                return has_faq(v)
                    elif isinstance(o, list):
                        for item in o:
                            if has_faq(item):
                                return True
                    return False
                if has_faq(data):
                    faq_evidence = True
            except Exception:
                pass
    software_evidence = bool(re.search(r"\b(software|application|app)\b", (p.get("text", "") or "").lower()) and bool(p.get("author")))
    if has_date_author and (is_article or bool(re.search(r"datepublished|author", block_text))):
        chosen_type = "Article"
        evidence_str = "Article: date/author found in page or JSON-LD; path or meta suggests article."
    elif faq_evidence:
        chosen_type = "FAQPage"
        evidence_str = "FAQPage: JSON-LD contains FAQPage/mainEntity; real Q&A structure detected."
    elif software_evidence:
        chosen_type = "SoftwareApplication"
        evidence_str = "SoftwareApplication: page text mentions software/application and has author/detail evidence."
    elif is_article:
        chosen_type = "WebPage"
        evidence_str = "WebPage: path suggests article but no date/author evidence found; using WebPage."
    elif p.get("h1") and p.get("title"):
        chosen_type = "WebSite"
        evidence_str = "WebSite: homepage-like content (no specific article/software/FAQ evidence)."
    else:
        chosen_type = "WebPage"
        evidence_str = "WebPage: default for general page with no stronger type evidence."
    d = {"@context": "https://schema.org", "@type": chosen_type}
    if chosen_type == "Article":
        d["headline"] = p.get("h1") or p.get("title") or ""
        d["description"] = desc or (p.get("excerpt", "") or "")
        d["url"] = final_url
        if p.get("published"):
            d["datePublished"] = p.get("published")
        if p.get("author"):
            d["author"] = {"@type": "Person", "name": p.get("author")}
        img = meta.get("og", {}).get("image")
        if img:
            d["image"] = img[0] if isinstance(img, list) else img
    elif chosen_type == "FAQPage":
        d["name"] = p.get("h1") or p.get("title") or ""
        d["url"] = final_url
        d["mainEntity"] = []
        evidence_str += " (mainEntity is empty skeleton until real Q&A pairs are added)"
    elif chosen_type == "Organization" or chosen_type == "WebSite":
        d["name"] = p.get("title") or p.get("h1") or ""
        d["url"] = final_url
        d["description"] = desc or ""
        img = meta.get("og", {}).get("image")
        if img:
            d["image"] = img[0] if isinstance(img, list) else img
    else:
        if chosen_type == "SoftwareApplication":
            d["name"] = p.get("title") or p.get("h1") or ""
            d["description"] = desc or ""
            d["url"] = final_url
        else:
            d["name"] = p.get("h1") or p.get("title") or ""
            d["description"] = desc or ""
            d["url"] = final_url
    skeleton_text = json.dumps(d, indent=2)
    snippet = f"# JSON-LD type selected: {chosen_type}\nEvidence: {evidence_str}\nOnly fields with real values included; no placeholders like 'WRITE A SUMMARY'.\n\n```json\n{skeleton_text}\n```"
    return snippet

def build_snippet_robots(origin: str, robots_text: str, include_sitemap=True, include_bot_allow=False) -> str:
    snippet = ""
    if include_sitemap:
        snippet += f"Sitemap: {origin}/sitemap.xml\n"
    if include_bot_allow:
        snippet += "# Optional AI-bot Allow stanza (labelled optional):\n"
        snippet += "# User-agent: OAI-SearchBot\n# User-agent: PerplexityBot\n# Allow: /\n"
        snippet += "# Note: optional; only include if you want to allow these bots.\n"
    return snippet

def build_snippet_llms(origin: str, links: list) -> str:
    top10 = links[:10] if links else []
    snippet = "# llms.txt skeleton (optional)\n"
    snippet += f"# Source: {origin}\n"
    snippet += "# Descriptions derived from internal links (link text approximated from URL path)\n"
    for idx, link in enumerate(top10, 1):
        text = link.get("text", "") if isinstance(link, dict) else ""
        url = link.get("url", link) if isinstance(link, dict) else link
        desc = text if text else url.replace(origin, "").lstrip("/") or "Homepage"
        snippet += f"- [{desc}]({url})\n"
    snippet += "# Note: optional; label clearly. Include only top 10 by position."
    return snippet

def build_agent_prompt(url: str, finding: dict, snippet_text: str) -> str:
    prompt = (
        f"Page URL: {url}\n"
        f"Evidence: {finding.get('evidence', '')}\n"
        f"Desired fix: {finding.get('fix', '')}\n"
        f"Snippet (ready-to-paste):\n{snippet_text}\n"
        f"How to verify: {finding.get('verify', '')}\n"
        "Apply the snippet from evidence above. Ensure any backticks are kept intact when pasting."
    )
    return prompt

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
                merged.append(Finding(f1.area, f1.sev, "Work the query terms into your title and H1", f1.evidence, f1.fix, f1.affects, f1.ties, confidence=f1.confidence, verify=f1.verify))
                skip_next = True
                continue
        merged.append(f)

    # 3. Order by severity, then 'both'
    order = {"high": 0, "med": 1, "low": 2, "info": 3}
    merged.sort(key=lambda f: (order.get(f.sev, 3), 0 if f.affects == "both" else 1))

    return [f.title for f in merged[:3]]


# --------------------------------------------------------------------------- the audit
def audit(url: str, query: str | None, tf, do_raw=True, country: str | None = None, language: str | None = None, raw_fn=None, note: str | None = None) -> dict:
    raw_fn = raw_fn or raw_view
    F: list[Finding] = []

    def add(*a, **k):
        F.append(Finding(*a, **k))

    url, internal_note = normalize_target(url)
    url_note = note or internal_note

    p = urlparse(url)
    host, origin = p.netloc.lower(), f"{p.scheme}://{p.netloc}"
    PUBLIC_SUFFIXES = {"co.uk", "com.au", "co.in", "vercel.app", "netlify.app", "github.io", "pages.dev", "onrender.com", "herokuapp.com"}
    def get_brand_token(hostname: str) -> str:
        parts = hostname.split(".")
        # Find registrable domain label
        # Try removing suffixes from end
        if len(parts) >= 3:
            # Check two-part suffixes
            suffix2 = ".".join(parts[-2:])
            if suffix2 in PUBLIC_SUFFIXES:
                return parts[-3]
            # Check one-part suffixes that are hosting platforms
            suffix1 = parts[-1]
            if suffix1 in PUBLIC_SUFFIXES:
                return parts[-2]
        # For normal domains: label before TLD (e.g., wikipedia.org -> wikipedia, python.org -> python)
        # Skip language subdomains (en, de) if they are first part and domain has 3+ parts
        if len(parts) >= 3:
            # If first part is a language code (2 chars), skip it
            first = parts[0]
            if len(first) == 2 and first.isalpha():
                return parts[-2] if len(parts) >= 3 else parts[-2]
        return parts[-2] if len(parts) >= 2 else parts[-1]
    brand_token = get_brand_token(host)

    # ---- FETCH 1: the page as an AI tool extracts it (rendered, live)
    http_fallback_used = False
    if url.startswith("https://"):
        try:
            got, errs = tf.fetch([url], links=True, image_links=True, page_metadata=True)
            e0 = next(iter(errs.values()), {})
            err_msg = str(e0.get("error", "")).lower()
            if not got and any(term in err_msg for term in ("ssl", "connect", "certificate")):
                http_url = "http://" + url[len("https://"):]
                got_http, errs_http = tf.fetch([http_url], links=True, image_links=True, page_metadata=True)
                if got_http:
                    got, errs = got_http, errs_http
                    url = http_url
                    http_fallback_used = True
                    p = urlparse(url)
                    host, origin = p.netloc.lower(), f"{p.scheme}://{p.netloc}"
        except (requests.RequestException, Exception) as ex:
            is_conn_or_ssl = isinstance(ex, (requests.ConnectionError, requests.exceptions.SSLError)) or any(
                term in str(ex).lower() for term in ("ssl", "connection", "connect", "certificate")
            )
            if is_conn_or_ssl:
                http_url = "http://" + url[len("https://"):]
                try:
                    got, errs = tf.fetch([http_url], links=True, image_links=True, page_metadata=True)
                    url = http_url
                    http_fallback_used = True
                    p = urlparse(url)
                    host, origin = p.netloc.lower(), f"{p.scheme}://{p.netloc}"
                except Exception:
                    f_crit = Finding("access", "high", "Main page could not be fetched",
                                     f"The TinyFish API returned a persistent error after retries: {str(ex).splitlines()[0]}.",
                                     "Check your network connection and the TinyFish API status. If the issue persists, verify the URL is accessible to AI crawlers.", "both",
                                     "The primary page content is unavailable, making a full audit impossible.",
                                     confidence="unverified", verify=f'curl -sI "{url}"')
                    return {"status": "error", "url": url, "url_note": url_note, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                            "scores": {"ai_readability": None, "search_visibility": None}, "findings": [asdict(f_crit)], "calls": tf.calls}
            else:
                f_crit = Finding("access", "high", "Main page could not be fetched",
                                 f"The TinyFish API returned a persistent error after retries: {str(ex).splitlines()[0]}.",
                                 "Check your network connection and the TinyFish API status. If the issue persists, verify the URL is accessible to AI crawlers.", "both",
                                 "The primary page content is unavailable, making a full audit impossible.",
                                 confidence="unverified", verify=f'curl -sI "{url}"')
                return {"status": "error", "url": url, "url_note": url_note, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                        "scores": {"ai_readability": None, "search_visibility": None}, "findings": [asdict(f_crit)], "calls": tf.calls}
    else:
        try:
            got, errs = tf.fetch([url], links=True, image_links=True, page_metadata=True)
        except (requests.RequestException, Exception) as ex:
            f_crit = Finding("access", "high", "Main page could not be fetched",
                             f"The TinyFish API returned a persistent error after retries: {str(ex).splitlines()[0]}.",
                             "Check your network connection and the TinyFish API status. If the issue persists, verify the URL is accessible to AI crawlers.", "both",
                             "The primary page content is unavailable, making a full audit impossible.",
                             confidence="unverified", verify=f'curl -sI "{url}"')
            return {"status": "error", "url": url, "url_note": url_note, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "scores": {"ai_readability": None, "search_visibility": None}, "findings": [asdict(f_crit)], "calls": tf.calls}

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
                       "No content is available at this URL (404 observed by TinyFish Fetch). It cannot be quoted by AI fetchers until it responds with content; links to it have no target to cite.",
                       confidence="likely", verify=f'curl -sI "{url}"')
        return {"status": "not_found", "url": url, "url_note": url_note, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "scores": {"ai_readability": None, "search_visibility": None}, "findings": [asdict(f404)], "calls": tf.calls}
    readable = bool(page and (page.get("text") or "").strip())
    if page is None or not readable:
        e = next(iter(errs.values()), {})
        if page is None:
            ev = f"TinyFish Fetch returned an error: {e.get('error')} (HTTP {e.get('status')})."
            fx = ("Check for bot blocking (WAF/Cloudflare rules), login/consent walls, or server errors. "
                  "Test with curl and a normal browser UA, then allow verified AI crawlers.")
        else:
            ev = "TinyFish Fetch returned an empty body."
            fx = ("Check for bot blocking (WAF/Cloudflare rules), login/consent walls, or content rendered only after user interaction. "
                  "Test with curl and a normal browser UA, then allow verified AI crawlers.")
        add("access", "high", "TinyFish Fetch could not extract any readable text from this page",
            ev, fx, "both",
            "This observation only covers TinyFish Fetch. A page unreadable by this fetcher may still be readable by other crawlers; it is not observed as absent from any search index.",
            confidence="likely", verify=f'curl -sL "{url}" | head -n 30')
        page = page or {}
    md = page.get("text") or ""
    meta = page.get("page_metadata") or {}
    final_url = page.get("final_url") or url
    title = clean(page.get("title"))
    if readable and looks_like_challenge(title, md, count_words(md)):
        add("access", "high", "Fetch was served a bot-challenge page, not your content",
            f"Title {title!r}, {count_words(md)} words, with challenge wording in the body.",
            "Allow verified crawlers through the WAF/bot protection for this path, then re-run. Until then the audit below describes the challenge page.", "both",
            "AI fetchers that hit the same wall see no content to quote or cite.",
            confidence="likely", verify=f'curl -sL "{url}" | head -n 30')
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
    parity = {"js_only_pct": None, "extraction_retention_pct": None, "headings_lost": []}
    if do_raw:
        try:
            raw = raw_fn(final_url)
        except Exception as ex:  # noqa: BLE001
            raw_err = str(ex)

    is_html_raw = raw and not raw.get("non_html")
    desc_src = "raw HTML" if raw and is_html_raw else "TinyFish Fetch"
    desc = raw["meta"].get("description", "") if (raw and is_html_raw) else (page.get("description") or "")

    if raw and not raw.get("non_html") and raw.get("truncated"):
        add("content", "info", "Page content truncated", f"The page exceeds 3MB and was truncated for analysis.", "No action needed unless critical content is only found at the very bottom of a huge page.", "ai",
            confidence="verified", verify=f'curl -sI "{final_url}" | grep -i content-length')

    # ---- query: given, or derived from the page itself
    query_src = "given"
    if not query:
        query, query_src = derive_query(title, h1), "derived from the page's own H1/title (tests findability by name only; pass -q for a real keyword test)"
    terms = key_terms(query)
    best_passage_result = best_passage(md, terms) if terms else None
    if best_passage_result:
        bp = best_passage_result
        if bp["coverage"] < 0.5:
            add("content", "med", "No passage answers the query directly",
                f"Best passage covers only {int(bp['coverage']*100)}% of query terms (at word {bp['offset_words']}). Suggest adding a direct 40-60 word answer under the H1.",
                "Write a concise 40-60 word answer that uses the query terms directly under the H1.", "search",
                confidence="likely", verify=f"Read first 600 words of {final_url}")
        if bp["offset_words"] > 600:
            add("content", "low", "The best answer sits deep in the page",
                f"Best passage starts at word {bp['offset_words']} (length {bp['length_words']} words).",
                "Move the core answer closer to the top, ideally within the first 600 words.", "ai",
                confidence="likely", verify=f"Read first 600 words of {final_url}")
    if not terms:
        add("content", "low", "Query terms could not be analysed",
            "The provided query contains no recognisable words (e.g. it is composed only of symbols or emojis).",
            "Provide a more descriptive query containing alphanumeric characters.", "search",
            confidence="verified", verify=f'echo "{query}"')

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

    # ---- SEARCH: up to three visibility queries (target, topic, brand)
    def query_params(extra=None):
        d = {"location": country} if country else {}
        if language:
            d["language"] = language
        if extra:
            d.update(extra)
        return d

    # Derive queries
    target_query = query or ""
    brand_query = brand_token
    # Topic: from title/H1 with brand token removed
    topic_raw = (h1 or title or "").strip()
    topic_tokens = [t for t in tokens(topic_raw) if t != brand_token and t not in STOP]
    topic_query = " ".join(topic_tokens) if topic_tokens else (h1 or title or "")
    # Skip rules
    skip_topic = norm_query(topic_query) == norm_query(target_query)
    skip_brand = norm_query(brand_query) == norm_query(target_query)
    # Skip brand query if token too short (<4 chars) or is a stopword/language code
    brand_token_short_or_stop = len(brand_token) < 4 or brand_token in STOP or (len(brand_token) == 2 and brand_token.isalpha())
    if brand_token_short_or_stop:
        # Log skip in call log
        tf.calls.append(("search", f"brand query skipped: token too short or language code ({brand_token})"))
        skip_brand = True

    visibility = {}
    # (a) target query
    target_serp, target_rank, target_entry = [], None, None
    try:
        target_pages = 2  # pages 0-2 -> up to 3 pages? Actually page range 3 means 0,1,2
        for pg in range(3):
            batch = tf.search(target_query, page=pg, **query_params())
            # Record call with filter info
            filter_used = "none"
            tf.calls[-1] = (tf.calls[-1][0], f"{target_query} (page {pg}; domain_filter={filter_used})")
            target_serp += batch
            for i, r in enumerate(batch, 1):
                if norm(r["url"]) in keys:
                    target_rank, target_entry = pg * len(batch) + i, r
                    break
            if target_rank or not batch:
                break
    except (requests.RequestException, Exception) as ex:
        add("visibility", "low", "Search unavailable, visibility not measured",
            f"The TinyFish Search API returned a persistent error: {str(ex).splitlines()[0]}.",
            "No action needed; re-run the audit later.", "search",
            confidence="unverified", verify="curl -sI https://api.search.tinyfish.ai/")

    # (b) topic query
    topic_serp, topic_rank, topic_entry = [], None, None
    if not skip_topic:
        try:
            for pg in range(2):  # pages 0-1
                batch = tf.search(topic_query, page=pg, **query_params())
                tf.calls[-1] = (tf.calls[-1][0], f"{topic_query} (page {pg}; domain_filter={filter_used})")
                topic_serp += batch
                for i, r in enumerate(batch, 1):
                    if norm(r["url"]) in keys:
                        topic_rank, topic_entry = pg * len(batch) + i, r
                        break
                if topic_rank or not batch:
                    break
        except (requests.RequestException, Exception):
            pass  # ignore errors for secondary queries
    else:
        topic_serp, topic_rank, topic_entry = [], None, None

    # (c) brand query
    brand_serp, brand_rank, brand_entry = [], None, None
    if not skip_brand:
        try:
            for pg in range(2):
                batch = tf.search(brand_query, page=pg, **query_params())
                tf.calls[-1] = (tf.calls[-1][0], f"{brand_query} (page {pg}; domain_filter={filter_used})")
                brand_serp += batch
                for i, r in enumerate(batch, 1):
                    if norm(r["url"]) in keys:
                        brand_rank, brand_entry = pg * len(batch) + i, r
                        break
                if brand_rank or not batch:
                    break
        except (requests.RequestException, Exception):
            pass
    else:
        brand_serp, brand_rank, brand_entry = [], None, None

    # Domain-restricted diagnostic with include_domains instead of site:
    own, own_entry = [], None
    if target_rank is None:
        try:
            own = tf.search(target_query, include_domains=host, **query_params({}))
            tf.calls[-1] = (tf.calls[-1][0], f"{target_query} (domain_filter=include_domains:{host})")
            own_entry = next((r for r in own if norm(r["url"]) in keys), None)
        except (requests.RequestException, Exception):
            pass

    # Set aliases for backward compatibility
    serp = target_serp
    rank = target_rank
    entry = target_entry
    seen = entry or own_entry
    if seen:
        seen = {**seen, "title": clean(seen.get("title")), "snippet": tidy_snippet(seen.get("snippet"))}

    # Note in report if CJK fallback was used
    if is_cjk(md):
        add("content", "info", "CJK matching fallback active",
            "This page appears to use a script without spaces (CJK). The auditor has switched to character bigram matching for terms and topic gaps.",
            "No action needed.", "ai",
            confidence="verified", verify=f'curl -sL "{final_url}" | head -n 30')

    # Record visibility results
    visibility = {
        "target": {"rank": target_rank, "serp": target_serp, "entry": target_entry},
        "topic": {"rank": topic_rank, "serp": topic_serp, "entry": topic_entry},
        "brand": {"rank": brand_rank, "serp": brand_serp, "entry": brand_entry},
    }

    # ---- competitors: fetch the pages that outrank us and profile them the same way
    cand = [(i, r) for i, r in enumerate(serp, 1) if norm(r["url"]).split("/")[0] != norm(url).split("/")[0]][:6]
    comps, comp_err, kept = [], [], []
    if cand:
        try:
            cg, ce = tf.fetch([r["url"] for _, r in cand], links=False, image_links=False, page_metadata=False)
        except (requests.RequestException, Exception) as ex:
            comp_err.append((0, "batch", f"Competitor fetch failed: {str(ex).splitlines()[0]}"))
            cg, ce = {}, {}

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
        hg, _ = tf.fetch(kept, format="html", include_selectors=["h1", "h2"], links=False, image_links=False, page_metadata=False)
        for c in comps:
            src = next((v for k, v in hg.items() if norm(k) == norm(c["url"]) or norm(v.get("final_url") or "") == norm(c["url"])), None)
            if src:
                o = Outline()
                o.feed(src.get("text") or "")
                c["h1"] = best_h1(o.headings, c["title"], c["h1"])
                c["t_h1"] = term_hits(terms, c["h1"])
                c["h2_texts"] = [t for l, t in o.headings if l == 2]


    # =========================================================== findings
    # -- access
    if http_fallback_used:
        add("access", "med", "Site is not served over HTTPS",
            "The site failed to connect over HTTPS due to an SSL/connection error, but responded over HTTP.",
            "Install a valid SSL/TLS certificate and configure HTTP requests to redirect to HTTPS.", "both",
            "Typical effect: search engines prioritize HTTPS and browsers show warnings for HTTP connections.",
            confidence="verified", verify=f'curl -sI "https://{p.netloc}"')
    if norm(final_url) != norm(url):
        add("access", "low", "URL redirects", f"{url} -> {final_url}", "Link internally and in sitemaps to the final URL.", "search",
            "Typical effect: redirect hops may dilute observed signals, and some fetchers may not follow them.",
            confidence="verified", verify=f'curl -sI "{url}" | grep -i location')
    if robots_txt:
        for role, sev, why in (("search", "high", "This bot builds the index AI search answers cite."),
                               ("user", "med", "This bot fetches the page when a user asks an AI assistant about it.")):
            bl = [b for b, r in BOTS.items() if r == role and blocked.get(b)]
            if bl:
                add("access", sev, f"robots.txt blocks {role} crawlers for this path", f"Blocked for: {', '.join(bl)} (path {path}).",
                    "Remove or narrow the matching Disallow rule in robots.txt, or add an explicit Allow for these agents.", "both", why,
                    confidence="verified", verify=f'curl -s "{origin}/robots.txt"')
        tr = [b for b, r in BOTS.items() if r == "training" and blocked.get(b)]
        if tr:
            add("access", "info", "Training crawlers are blocked (policy choice)", f"Blocked: {', '.join(tr)}.",
                "No action needed if intentional. Blocking these does not by itself hide you from AI search citations.", "ai",
                confidence="verified", verify=f'curl -s "{origin}/robots.txt"')
    tags = " ".join(dict.fromkeys(" ".join([str(meta.get("robots") or ""), (raw or {}).get("meta", {}).get("robots", ""), (raw or {}).get("x_robots", "")]).lower().split()))
    if "noindex" in tags:
        add("access", "high", "Page is marked noindex", f"Robots directives seen: {tags.strip()}", "Remove noindex (meta robots or X-Robots-Tag header) if you want this page found.", "both",
            "Engines drop noindex pages from results entirely.",
            confidence="verified", verify=f'curl -sI "{final_url}" | grep -i x-robots-tag')
    if re.search(r"nosnippet|max-snippet:\s*0", tags):
        add("access", "med", "Snippet use is forbidden", f"Robots directives seen: {tags.strip()}", "Remove nosnippet / max-snippet:0 so engines and AI answers may quote you.", "both",
            "Typical effect: no snippet permission reduces quoted text and click-through.",
            confidence="verified", verify=f'curl -sI "{final_url}" | grep -i x-robots-tag')
    if readable:
        can = meta.get("canonical")
        if not can:
            add("metadata", "med", "No canonical URL", "page_metadata has no canonical.", f'Add <link rel="canonical" href="{final_url}"> in <head>.', "search",
                "Without it, duplicate/parameter URLs compete with each other for the same query.",
                confidence="verified", verify=f'curl -sL "{final_url}" | grep -i \'rel="canonical"\'')
        elif norm(can) != norm(final_url):
            add("metadata", "high", "Canonical points to a different URL", f"canonical={can}  but fetched page is {final_url}",
                "Point the canonical at this URL, or expect the other URL to be preferred by the search engine rather than this one.", "search",
                "Typical effect: a preferred canonical may cause this URL to not appear as the representative result.",
                confidence="verified", verify=f'curl -sL "{final_url}" | grep -i \'rel="canonical"\'')
    if sm_found is False:
        add("access", "med", "Page not found in sitemap", f"Checked {sm_checked} URLs across {len(sm_got)} sitemap file(s).", "Add the page to sitemap.xml with an accurate <lastmod>.", "search",
            "Discovery then depends on internal links alone.",
            confidence="likely", verify=f'curl -sL "{sm_urls[0]}" | grep -i "{p.path}"')
    elif sm_found is None and smaps:
        why = next(iter(sm_err.values()), {}).get("error", "no URLs in the response")
        add("access", "low", "Declared sitemap could not be read", f"robots.txt points to {', '.join(sm_urls[:2])} but Fetch got no usable URL list ({why}).",
            "Open it in a browser to confirm it works. This may be a Fetch limitation, so treat it as unverified.", "search",
            confidence="unverified", verify=f'curl -sL "{sm_urls[0]}" | head -n 20')
    elif sm_found is None:
        add("access", "med", "No sitemap found", f"{sm_urls[0]} returned no URL list (missing, or a single-page app answering with its home page).",
            "Publish sitemap.xml and reference it from robots.txt with a Sitemap: line.", "search", "Discovery then depends on internal links alone.",
            confidence="likely", verify=f'curl -sI "{origin}/sitemap.xml"')
    if not llms_txt:
        add("access", "low", "No llms.txt", f"{origin}/llms.txt returned nothing.",
            "Optional: publish llms.txt listing key pages with one-line descriptions. No major engine documents this as a ranking signal, so treat it as a typical optional convention.", "ai",
            confidence="verified", verify=f'curl -sI "{origin}/llms.txt"')

    # -- content (what AI tools can read)
    if readable:
        if me["words"] < 150:
            words_ev = (f"Fetch rendered the page in a real browser and extracted {me['words']} words (threshold is 150 words)."
                        if me["words"] > 0 else "Fetch rendered the page in a real browser and extracted 0 words.")
            add("content", "high", f"Only {me['words']} words extracted", words_ev,
                "Put the substantive answer in HTML text, not images/canvas/widgets.", "both", "Typical effect: little extractable text reduces what an engine or AI tool can observe and match against a query.",
                confidence="likely", verify=f'curl -sL "{final_url}" | wc -w')

        if raw and not raw.get("non_html"):
            # Skip rules: non-HTML handled by non_html; challenge pages skipped below.
            raw_words = len(re.findall(r"\w+", raw["text"] or ""))
            is_challenge = looks_like_challenge(title, md, count_words(md))
            skip_parity = raw_words < 100 or is_challenge or raw.get("non_html")
            # Note: non-HTML already excluded from this branch; skip_parity covers <100 words and challenge.
            if not skip_parity:
                ret = extraction_retention(raw["text"], md)
                if ret is not None and ret < 0.5 and raw_words >= 300:
                    # Find missing passages (use raw shingles that are missing from md as examples)
                    raw_shingles = shingle_words(raw["text"], 5)
                    md_shingles = shingle_words(md, 5)
                    missing_shingles = sorted(raw_shingles - md_shingles)
                    # Build 3 example passages (12 words each) from raw text not present in md
                    passages = []
                    toks = tokens(raw["text"])
                    for i in range(0, len(toks)-12, 3):
                        passage = " ".join(toks[i:i+12])
                        if clean(passage) not in md:
                            passages.append(passage)
                        if len(passages) >= 3:
                            break
                    examples = passages[:3]
                    ev = f"AI extraction keeps only about {round(ret*100)}% of the text in the page's HTML"
                    evidence_str = ev + ". Evidence: " + "; ".join(f'"{ex}"' for ex in examples)
                    add("content", "med", "AI extraction keeps only a fraction of the HTML text",
                        evidence_str,
                        "Ensure the main content is in the static HTML, not injected after load.", "both",
                        "Some crawlers read only raw HTML, so text missing from it may not be observed. This does not prove exclusion from any index.",
                        confidence="likely", verify=f'curl -sL "{final_url}" | wc -w')
                # Headings lost
                lost_heads = headings_lost(dom_heads, md)
                h1_only_missing = False
                if lost_heads:
                    # Check if only H1 is missing (other headings present in md?)
                    md_heads = [t for l, t in md_stats(md)[0] if l in (1, 2, 3)]
                    if len(lost_heads) == 1 and clean(lost_heads[0]).lower() == clean(next((t for lvl, t in dom_heads if lvl == 1), "")).lower():
                        h1_only_missing = True
                    if h1_only_missing:
                        add("content", "info", "The extractor's markdown omitted the H1 although it is in the HTML",
                            "The extractor's markdown omitted the H1 although it is in the HTML. This may be an extraction quirk, not a page problem.",
                            "No fix needed for the page; this is an extraction quirk.", "ai",
                            confidence="likely", verify=f'curl -sL "{final_url}" | grep -i "<h1"')
                    else:
                        add("content", "low", "Headings lost in AI extraction", f"H1-H3 headings present in the raw HTML but missing from the Fetch markdown: {', '.join(lost_heads[:5])}.",
                            "Keep headings in plain HTML so extractors see them.", "ai",
                            "Headings guide extractors; missing headings reduce observed topic structure.",
                            confidence="likely", verify=f'curl -sL "{final_url}" | grep -iE "<h[1-3]"')
            # Compute parity regardless of skip for JSON/table
            raw_words = len(re.findall(r"\w+", raw["text"] or ""))
            ret_val = extraction_retention(raw["text"], md) if (raw_words >= 100 and not is_challenge) else None
            lost_heads = headings_lost(dom_heads, md) if (raw_words >= 100 and not is_challenge) else []
            parity = {
                "js_only_pct": round((1 - (cov if ('cov' in locals() and cov is not None) else 1)) * 100) if 'cov' in locals() else None,
                "extraction_retention_pct": round(ret_val * 100) if ret_val is not None else None,
                "headings_lost": lost_heads,
            }
            # Actually compute js_only_pct properly
            # Re-calculate coverage for parity if needed
            # We'll set it after coverage is computed.
        else:
            ret_val = None
            lost_heads = []
            parity = {"js_only_pct": None, "extraction_retention_pct": None, "headings_lost": []}

        # Re-calc coverage for parity (only when raw exists)
        if raw and not raw.get("non_html"):
            cov, miss = coverage(md, raw["text"])
            if 'parity' not in locals():
                parity = {"js_only_pct": None, "extraction_retention_pct": None, "headings_lost": []}
            js_only = round((1 - cov) * 100) if cov is not None else None
            # Original JS-dependence finding (only once)
            absent_pct = round((1 - cov) * 100) if cov is not None else None
            if cov is not None:
                # Between 15% and 30%: only parity table line, no finding
                if absent_pct > 60:
                    sev = "high"
                elif absent_pct >= 30:
                    sev = "med"
                elif absent_pct >= 15:
                    sev = None  # only parity line
                else:
                    sev = None  # no finding below 30%
                # Build original-text passage example (not normalized) for display and verify
                # Use a window from the original rendered md that is missing from raw HTML
                original_passage_example = ""
                if miss:
                    # Pick the shortest missing passage that has 6+ words and no digits for verify fragment
                    candidates = []
                    for m in miss:
                        words = re.findall(r"[a-zA-Z]+", m)
                        if len(words) >= 6 and not re.search(r"\d", m):
                            candidates.append(m)
                    if candidates:
                        original_passage_example = candidates[0]
                    else:
                        original_passage_example = miss[0] if miss else ""
                # Verify command uses original fragment (4-5 words) with grep -i -F
                verify_fragment = ""
                if original_passage_example:
                    words = original_passage_example.split()
                    # Pick 4-5 word fragment from original text (not normalized)
                    if len(words) >= 4:
                        verify_fragment = " ".join(words[:5])
                    else:
                        verify_fragment = original_passage_example
                # Confidence: verified only if raw GET text confirmed absence of exact fragment
                # Since coverage already compares against raw_text (normalized), we treat it as verified when raw exists and is not a challenge/non-html.
                confidence = "verified" if (raw and not raw.get("non_html") and not is_challenge and raw_words >= 100) else "likely"
                if sev:
                    # Show example passage in original text (casing and punctuation kept)
                    display_example = original_passage_example[:200] if original_passage_example else (miss[0][:200] if miss else "")
                    evidence_str = (f"{absent_pct}% of sampled passages from the rendered page are absent from the raw HTML. "
                                     f"Example passage missing from raw HTML: \"{display_example}\"" if display_example else f"{absent_pct}% of sampled passages absent.")
                    add("content", sev, "Much of the text only exists after JavaScript runs",
                        evidence_str,
                        "Server-side render or statically generate the main content (SSR/SSG). Verify with `grep -i -F`.", "both",
                        "Typical effect: some crawlers read only raw HTML, so text that requires JavaScript may not be observed by them. This is not proof that the page is excluded from any index.",
                        confidence=confidence, verify=f'grep -i -F "{verify_fragment}" <(curl -sL "{final_url}")' if verify_fragment else f'curl -sL "{final_url}" | head -n 30')
            # Skip rules for retention/headings
            raw_words = len(re.findall(r"\w+", raw["text"] or ""))
            is_challenge = looks_like_challenge(title, md, count_words(md))
            skip_parity = raw_words < 100 or is_challenge
            ret_val = extraction_retention(raw["text"], md) if (not skip_parity) else None
            lost_heads = headings_lost(dom_heads, md) if (not skip_parity) else []
            parity["js_only_pct"] = js_only
            parity["extraction_retention_pct"] = round(ret_val * 100) if ret_val is not None else None
            parity["headings_lost"] = lost_heads
            # JSON-LD checks (original)
            if not raw["jsonld"]:
                add("content", "med", "No structured data (JSON-LD)", "No application/ld+json block in the raw HTML.",
                    "Add JSON-LD. Starter based on what Fetch extracted:\n" + jsonld_skeleton(me, desc, meta, final_url,
                    (meta.get("og") or {}).get("type") == "article" or bool(re.search(r"/(blog|news|articles?|posts?|guides?)/", path))), "both",
                    "Typical effect: structured data gives extractors unambiguous facts instead of inferred ones.",
                    confidence="verified", verify=f'curl -sL "{final_url}" | grep -i "application/ld+json"')
            else:
                types, bad, gaps = ld_summary(raw["jsonld"])
                if bad:
                    add("content", "med", "Invalid JSON-LD", f"{bad} block(s) fail to parse.", "Validate with a JSON linter and Google's Rich Results Test.", "both",
                        confidence="verified", verify=f'curl -sL "{final_url}" | grep -i "application/ld+json"')
                for t, m in gaps:
                    add("content", "low", f"{t} JSON-LD is missing properties", f"Missing: {', '.join(m)}.", f"Add {', '.join(m)} to the {t} object.", "both",
                        confidence="verified", verify=f'curl -sL "{final_url}" | grep -i "application/ld+json"')
        elif raw and raw.get("non_html"):
            add("access", "low", "Not an HTML page", f"Content-Type is {raw['content_type']}.", "No action needed if this is intentional (e.g. PDF). If this should be a web page, check your server configuration.", "both",
                confidence="verified", verify=f'curl -sI "{final_url}" | grep -i content-type')
        elif raw_err:
            add("content", "info", "Raw HTML check skipped", raw_err, "Re-run with network access to the page.", "ai",
                confidence="unverified", verify=f'curl -sI "{final_url}"')
        heads = dom_heads or md_stats(md)[0]
        h1s = [t for l, t in heads if l == 1]
        if not h1s:
            add("content", "med", "No H1 in extracted content", f"No <h1> found {'in the rendered HTML' if dom_heads else 'in the extracted content'}.", "Add one H1 stating the page's main topic in plain words.", "both",
                "Typical effect: the H1 is the main label extractors use for page topic.",
                confidence="likely", verify=f'curl -sL "{final_url}" | grep -i "<h1"')
        elif len(h1s) > 1:
            add("content", "low", f"{len(h1s)} H1s", "; ".join(h1s[:4]), "Keep one H1; demote the rest to H2.", "ai",
                confidence="likely", verify=f'curl -sL "{final_url}" | grep -i "<h1"')
        sk = first_skip(heads)
        if sk:
            (la, ta), (lb, tb) = sk
            add("content", "low", "Heading levels skip", f'H{la} "{ta[:50]}" is followed directly by H{lb} "{tb[:50]}".',
                "Use consecutive heading levels so sections nest cleanly.", "ai",
                confidence="likely", verify=f'curl -sL "{final_url}" | grep -iE "<h[1-6]"')
        real = [i for i in images if not i["decorative"]]
        noalt = [i for i in real if i["alt"] is None]
        if len(real) >= 3 and len(noalt) / len(real) > 0.3:
            add("content", "med", f"{len(noalt)} of {len(real)} content images have no alt attribute",
                "Examples: " + ", ".join(i["src"][:70] for i in noalt[:3]), "Add descriptive alt text (alt=\"\" only for purely decorative images).", "both",
                "Without alt text, AI fetchers that read this page do not observe the image content; typical effect is that image-related information is missing from any answer drawn from this page.",
                confidence="likely", verify=f'curl -sL "{final_url}" | grep -i "<img"')
        if is_article and not (page.get("published_date") and page.get("author")):
            miss = [k for k, v in (("published date", page.get("published_date")), ("author", page.get("author"))) if not v]
            add("content", "med", f"Article exposes no {' or '.join(miss)}", "TinyFish Fetch found none, so an AI tool reading the page will not have it either.",
                "Show byline and date in visible HTML and mirror them in Article JSON-LD (datePublished, dateModified, author).", "both",
                "Typical effect: freshness and attribution help AI answers evaluate whether to cite a page.",
                confidence="likely", verify=f'curl -sL "{final_url}" | grep -iE "datePublished|author"')

        # -- metadata
        if not title or len(title) < 20 or len(title) > 65:
            site_name = (meta.get("og") or {}).get("site_name", "")
            idea = suggest_title(f"{h1} - {query}" if h1 and me["t_title"] is not None and me["t_title"] < 1 else (h1 or title or query), title, site_name)
            add("metadata", "med", "Title tag length" if title else "Missing title tag", f"Title ({len(title)} chars): {title!r}. Aim for roughly 30-60.",
                "Rewrite it to state the page topic in plain words." + (f" Starting point (edit it): {idea}" if idea and idea != title else ""), "search",
                "Typical effect: the title is a primary relevance signal and the clickable headline in results.",
                confidence="verified", verify=f'curl -sL "{final_url}" | grep -i "<title"')
        if not desc:
            add("metadata", "med", "No meta description", f"None found (source: {desc_src}).", "Write a 120-160 character summary containing the primary query terms.", "search",
                "Typical effect: without a meta description the engine may invent a snippet from body text.",
                confidence="verified", verify=f'curl -sL "{final_url}" | grep -i \'<meta name="description"\'')
        elif not 70 <= len(desc) <= 160:
            add("metadata", "low", "Meta description length", f"{len(desc)} chars.", "Aim for 120-160 characters.", "search",
                confidence="verified", verify=f'curl -sL "{final_url}" | grep -i \'<meta name="description"\'')
        og = meta.get("og") or {}
        og_miss = [k for k in ("title", "description", "image") if not og.get(k)]
        twitter_card = (meta.get("twitter") or {}).get("card")

        if og_miss:
            miss_list = [f"og:{k}" for k in og_miss]
            if not twitter_card:
                miss_list.append("twitter:card")

            add("metadata", "low", "Open Graph tags incomplete",
                f"Missing {', '.join(miss_list)}.",
                "Add the missing og: tags; link previews and many AI tools use them.", "both",
                confidence="verified", verify=f'curl -sL "{final_url}" | grep -i \'property="og:\'')
        elif not twitter_card:
            pass
        if not meta.get("viewport"):
            add("metadata", "low", "No viewport meta tag", "", 'Add <meta name="viewport" content="width=device-width, initial-scale=1">.', "search", "Typical effect: mobile-first indexing relies on viewport settings.",
                confidence="verified", verify=f'curl -sL "{final_url}" | grep -i \'name="viewport"\'')
        if not page.get("language"):
            add("metadata", "low", "No language declared", "", 'Add <html lang="en"> (or the right code).', "ai",
                confidence="verified", verify=f'curl -sL "{final_url}" | grep -i "<html"')

    # -- visibility (Search) tied to readability (Fetch)
    if rank is None:
        if own_entry:
            add("visibility", "med" if readable and not any(blocked.get(b) for b in BOTS) else "high", f"Returned by a domain-restricted TinyFish Search, but not in the top {len(serp)} results for this query (observed at {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC)",
                f"Returned by a TinyFish domain-restricted search (include_domains:{host}) as \"{own_entry['title']}\" (observed at {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC), but not returned in the top {len(serp)} results of this observation for \"{query}\".",
                "This observation shows the page is returned by a domain-restricted TinyFish Search but does not rank in the top results for this query. Compare with the pages above for structural differences.", "search",
                confidence="likely", verify=f'Search for "{query}" on Google or Bing')
        else:
            add("visibility", "high" if not readable or any(blocked.get(b) for b in BOTS) else "med", "Not returned in the top results for a domain-restricted TinyFish Search",
                f"Not returned by TinyFish Search for \"{query}\" (top {len(serp)} results checked at {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC) or for a domain-restricted search (include_domains:{host}). This does not show it is absent from other search engines.",
                "Check noindex, canonical, robots.txt, sitemap and internal links; then consider requesting indexing in Search Console or Bing Webmaster Tools (this observation is only for TinyFish Search).", "both",
                "Typical effect: if a page is not returned by this observed search set, it is unlikely to appear in answers drawn from this source; other engines may still include it.",
                confidence="likely", verify=f'Search "site:{host}" on Google or Bing')
    elif rank > 10:
        add("visibility", "med", f"Ranks #{rank} for \"{query}\"", "Below the first results page.", "See the comparison table below.", "search",
            confidence="likely", verify=f'Search for "{query}" on Google or Bing')
    if seen:
        st, sn = seen.get("title", ""), seen.get("snippet", "")
        if title_rewritten(st, title, h1):
            add("visibility", "med", "Search shows a different title than your <title>", f'Shown: "{st}"   Yours: "{title}"',
                "Engines rewrite titles that are too long, boilerplate-heavy, or that disagree with the H1. Make title and H1 say the same thing in under ~60 characters.", "search",
                "Typical effect: a rewritten title suggests the engine preferred its own description for this query.",
                confidence="likely", verify=f'Compare search title with: curl -sL "{final_url}" | grep -i "<title"')
        src = snippet_source(sn, desc, md)
        if src == "page body" and readable:
            add("visibility", "low", "Snippet is pulled from body text, not your meta description", f"Shown: '{sn[:160]}'",
                "Rewrite the meta description to answer the query directly, using its key terms." if desc else "Add a meta description.", "search",
                confidence="likely", verify=f'Compare search snippet with: curl -sL "{final_url}" | grep -i \'<meta name="description"\'')
    if terms and readable:
        cmp_ = lambda k: avg([c[k] for c in comps])
        for key, label, sev in (("t_title", "title tag", "high"), ("t_h1", "H1", "med"), ("t_first", "first 120 words", "med")):
            mine, theirs = me[key], cmp_(key)
            if mine is not None and mine < 1 and theirs is not None and theirs - mine >= 0.34:
                miss = [t for t in terms if not term_hits([t], {"t_title": title, "t_h1": h1, "t_first": " ".join(md.split()[:120])}[key])]
                add("visibility", sev, f"Query terms missing from your {label}", f"Missing: {', '.join(miss)}. Top results cover {round(theirs * 100)}% of the terms there; you cover {round(mine * 100)}%.",
                    f"Work {', '.join(miss)} into the {label} where it reads naturally.", "search",
                    "Extractors weigh title/H1/opening text most when deciding what a page answers.",
                    confidence="likely", verify=f'curl -sL "{final_url}" | grep -i "{miss[0]}"')

        # Topic Gap Analysis
        if len(comps) >= 2:
            gaps = extract_topic_gaps({"text": md, "dom_heads": dom_heads}, comps, terms)
            if gaps:
                sev = "med" if len(gaps) >= 3 else "low"
                counts = []
                for g in gaps:
                    count = sum(1 for c in comps if any(sim(g, h) >= 0.6 for h in c.get("h2_texts", [])))
                    counts.append(f"{g} (covered by {count} of {len(comps)})")

                add("visibility", sev, "Topics the top pages cover that yours does not",
                    f"Topics missing: {', '.join(counts)}.",
                    "Add sections covering these topics only where they genuinely apply to your content.", "search",
                    confidence="likely", verify=f'Compare "{final_url}" headings with competitors')
                me["topic_gaps"] = gaps

        mw = median([c["words"] for c in comps])
        if mw and me["words"] < 0.5 * mw:

            add("visibility", "med", "Much thinner than the pages that outrank you", f"You: {me['words']} words. Median of top results: {mw}.",
                "Cover the sub-questions those pages answer (see their H2s). Length itself is not the goal; coverage is.", "search",
                confidence="likely", verify=f'curl -sL "{final_url}" | wc -w')
        if comps and sum(1 for c in comps if c["tables"] or c["lists"]) >= 2 and not (me["tables"] or me["lists"]):
            add("visibility", "low", "Competitors use lists/tables; you use neither", f"{sum(1 for c in comps if c['tables'])} of {len(comps)} have tables, {sum(1 for c in comps if c['lists'])} have lists.",
                "Put comparable facts into a table or list; both are easy for AI tools to lift into an answer.", "search",
                confidence="likely", verify=f'curl -sL "{final_url}" | grep -iE "<table|<ul|<ol"')
        dated = sum(1 for r in serp[:5] if r.get("date"))
        if is_article and dated >= 2 and not page.get("published_date"):
            add("visibility", "med", "Top results show dates; your page exposes none", f"{dated} of the top 5 results carry a date in search.", "Expose a visible publish/updated date and mirror it in JSON-LD.", "search",
                confidence="likely", verify=f'curl -sL "{final_url}" | grep -iE "date|published"')

    # ---- New visibility observations using observed positions only
    # Found for brand but not target/topic
    if readable:
        if brand_rank is not None and target_rank is None and not skip_brand:
            add("visibility", "med", "Found for the brand name but not for what the page is about",
                f"The page appears in brand results (rank {brand_rank}) but is not observed in the top results for the target query.",
                "Review whether the page's content clearly addresses the target topic; align title/H1 and opening text with it.", "search",
                confidence="likely", verify=f'Search for "{brand_query}" and "{target_query}"')
        # Another page of same host returned for target query
        same_host_target = [r for r in target_serp if host in r.get("url", "") and norm(r.get("url", "")) not in keys]
        if same_host_target:
            urls_named = ", ".join(r.get("url", "") for r in same_host_target[:3])
            add("visibility", "med", "Another page of the same host is returned for the target query",
                f"Returned instead: {urls_named}.",
                "Check internal links and canonical settings so the intended page is preferred for this query.", "search",
                confidence="likely", verify=f'Search "{target_query}"')
        # Possible name overlap on other domains (title contains brand token as whole word)
        overlap = []
        if len(brand_token) >= 4:
            for r in target_serp:
                url_host = urlparse(r.get("url", "")).netloc.lower()
                if url_host and url_host != host:
                    title_words = set(tokens((r.get("title", "") or "").lower()))
                    if brand_token.lower() in title_words:
                        overlap.append(r.get("url", ""))
        if overlap:
            overlap_list = ", ".join(overlap[:3])
            add("visibility", "info", "Possible name overlap",
                f"Results on other domains whose title contains '{brand_token}': {overlap_list}.",
                "No assertion about ownership; just note potential name overlap.", "search",
                confidence="likely", verify=f'Search "{target_query}"')

    # =========================================================== scores (transparent heuristics)
    # Per-category breakdown
    cat_scores = {}
    for area in ["access", "content", "metadata", "visibility"]:
        penalty_sum = sum(PEN[f.sev] for f in F if f.area == area)
        cat_scores[area] = max(0, 100 - penalty_sum)

    if not readable:
        ai = vis = None
    else:
        ai = max(0, 100 - sum(PEN[f.sev] for f in F if f.affects in ("ai", "both")))
        base = 10 if rank is None else 95 if rank <= 3 else max(50, 80 - 3 * (rank - 4)) if rank <= 10 else 40
        vis = max(0, base - sum(PEN[f.sev] for f in F if f.affects == "search") // 2)
    order = {"high": 0, "med": 1, "low": 2, "info": 3}
    F.sort(key=lambda f: order[f.sev])

    # ---- Populate snippet and agent_prompt for each finding
    first_image_url = ""
    real_images = [i for i in images if not i.get("decorative")]
    if real_images:
        first_img = real_images[0]
        src = first_img.get("src", "")
        if src and src.startswith("/"):
            first_image_url = origin + src
        elif src:
            first_image_url = src
    links_list = page.get("links") or []
    for f in F:
        snippet_text = ""
        # Attach snippet only when it is the real fix for this finding
        if f.title == "No canonical URL" or (f.area == "metadata" and "canonical" in f.title and "points" not in f.title):
            snippet_text += build_snippet_canonical(final_url, meta) + "\n"
        elif f.title == "Missing title tag" or (f.area == "metadata" and ("Title tag" in f.title or "description" in f.title.lower())):
            snippet_text += build_snippet_title_meta(h1, title, query or "", terms, desc, final_url, meta, md) + "\n"
        elif f.title == "Open Graph tags incomplete" or (f.area == "metadata" and "Open Graph" in f.title):
            # Only missing tags, not full proposal
            snippet_text += build_snippet_og_twitter(title, desc, meta, final_url, first_image_url) + "\n"
        elif f.title == "No structured data (JSON-LD)" or f.title == "Invalid JSON-LD" or f.title.startswith("JSON-LD") or (f.area == "content" and "JSON-LD" in f.title):
            snippet_text += build_snippet_jsonld(me, desc, meta, final_url, is_article, raw["jsonld"] if raw and not raw.get("non_html") else [], path) + "\n"
        elif f.title == "No sitemap found" or f.title == "Page not found in sitemap":
            snippet_text += build_snippet_robots(origin, robots_txt or "", include_sitemap=True, include_bot_allow=False) + "\n"
        elif f.title == "No llms.txt":
            snippet_text += build_snippet_llms(origin, links_list) + "\n"
        elif f.title == "Declared sitemap could not be read":
            # Never emit Sitemap line or AI-bot stanza for this finding
            snippet_text = ""
        else:
            snippet_text = ""
        # Agent prompt: only when snippet exists; otherwise short inspect/verify prompt
        f.snippet = snippet_text
        if snippet_text:
            f.agent_prompt = build_agent_prompt(final_url, {
                "evidence": f.evidence,
                "fix": f.fix,
                "verify": f.verify,
            }, snippet_text)
        else:
            # Short inspect/verify prompt without "apply the snippet"
            verify_cmd = f.verify or f"curl -sL \"{final_url}\" | head -n 30"
            f.agent_prompt = f"Inspect the finding: {f.title}. Evidence: {f.evidence}. Verify with: {verify_cmd}"

    fix_first_titles = select_fix_first(F)

    return {
        "visibility": visibility,
        "url": url, "final_url": final_url, "url_note": url_note, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "query": query, "query_source": query_src, "country": country, "language": language, "rank": rank, "serp_entry": seen, "serp_size": len(serp),
        "scores": {"ai_readability": ai, "search_visibility": vis},
        "page": {**me, "title": title, "description": desc, "description_source": desc_src, "canonical": meta.get("canonical"),
                 "robots": tags.strip(), "images": len(images), "images_missing_alt": len([i for i in images if i["alt"] is None and not i["decorative"]]),
                 "links": len(set(page.get("links") or [])), "excerpt": md[:500],
                 "jsonld_types": ld_summary(raw["jsonld"])[0] if (raw and not raw.get("non_html")) else None, "raw_status": (raw or {}).get("status")},
        "site": {"robots_txt": bool(robots_txt), "llms_txt": bool(llms_txt), "in_sitemap": sm_found, "blocked_bots": [b for b, v in blocked.items() if v]},
        "competitors": comps, "competitor_errors": comp_err, "findings": [asdict(f) for f in F], "fix_first": fix_first_titles, "calls": tf.calls,
        "category_scores": cat_scores, "topic_gaps": me.get("topic_gaps", []),
        "parity": parity,
        "best_passage": best_passage_result,
        "terms": terms,
    }


# --------------------------------------------------------------------------- report
def to_markdown(r: dict) -> str:
    note_line = f"  \n_{r['url_note']}_" if r.get("url_note") else ""
    if r.get("status") == "not_found":
        f = r["findings"][0]
        return (f"# AI-readability & search audit\n\n**Page:** {r['url']}{note_line}  \n**Checked live:** {r['checked_at']}\n\n## Verdict\n\n"
                f"**Not auditable: this URL does not exist.** {f['evidence']}\n\n**Fix:** {f['fix']}\n\n"
                "No scores were computed, because there is no page to score.\n")
    P, S = r["page"], r["scores"]
    sm = {None: "unknown"}.get(r["site"]["in_sitemap"], r["site"]["in_sitemap"])
    ld = ", ".join(t if n == 1 else f"{t} (x{n})" for t, n in Counter(P["jsonld_types"] or []).items())
    esc = lambda s: str(s).replace("|", "\\|").replace("\n", " ")
    rank = f"position {r['rank']} in this observed result set" if r["rank"] else f"not returned in the top {r['serp_size']} results of this observation"
    o = [f"# AI-readability & search audit\n", f"**Page:** {r['final_url']}{note_line}  \n**Checked live:** {r['checked_at']}  \n"
         f"**Query:** \"{r['query']}\" ({r['query_source']})  \n**Observation context:** query = \"{r['query']}\"; country/language = {r.get('country') or 'not specified'}; result pages checked = up to {r['serp_size']} (pages 0-2); timestamp = {r['checked_at']}  \n",
         "## Verdict\n", f"- **AI readability:** {S['ai_readability']}/100", f"- **Search visibility for the query:** {rank} (score {S['search_visibility']}/100)",
         f"- **Findings:** {sum(f['sev'] == 'high' for f in r['findings'])} high, {sum(f['sev'] == 'med' for f in r['findings'])} medium, "
         f"{sum(f['sev'] == 'low' for f in r['findings'])} low\n",
         "| Category | Score | Findings |\n|---|---|---|\n" + "\n".join(
             f"| {area} | {s} | {sum(1 for f in r['findings'] if f['area'] == area)} |"
             for area, s in r.get("category_scores", {}).items()
         ) + "\n",
         "_Scores are transparent heuristics: readability = 100 minus finding penalties (high 15, medium 7, low 3); visibility starts from rank and is reduced by search-side findings. Category scores use the same penalties._\n",
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
         ]
    bp = r.get("best_passage")
    if bp and r.get("query"):
        passage_text = (bp.get("passage") or "")[:400]
        terms_for_highlight = [t for t in (r.get("terms") or []) if t and len(t) > 1]
        highlighted = passage_text
        for t in terms_for_highlight:
            # Word-boundary highlight to avoid matching inside other words (e.g. "ai" in "mailbox")
            pattern = r"(?<!\w)" + re.escape(t) + r"(?!\w)"
            highlighted = re.sub(pattern, lambda m: f"**{m.group(0)}**", highlighted, flags=re.IGNORECASE)
        o += [
            "## Best passage for this query\n",
            f"> {highlighted}\n",
            f"- Coverage: **{int((bp.get('coverage', 0) or 0)*100)}%** | Word offset from top: **{bp.get('offset_words', 'n/a')}** | Length: **{bp.get('length_words', 'n/a')} words**\n",
        ]
    o += ["## How it shows up (TinyFish Search)\n"]
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
        if f.get("confidence") or f.get("verify"):
            conf = f.get("confidence", "")
            ver = f.get("verify", "")
            if conf and ver:
                o.append(f"_Confidence: {conf} | Verify: {ver}_")
            elif conf:
                o.append(f"_Confidence: {conf}_")
            elif ver:
                o.append(f"_Verify: {ver}_")
        # Parity table next to JS-dependence finding
        if f.get("title") == "Much of the text only exists after JavaScript runs":
            p = r.get("parity", {})
            o.append("\n**Parity:** | Metric | Value |\n|---|---|\n")
            o.append(f"| JS-only % | {p.get('js_only_pct', 'n/a')}% |")
            o.append(f"| Extraction retention | {p.get('extraction_retention_pct', 'n/a')}% |")
            o.append(f"| Headings lost | {', '.join(p.get('headings_lost', [])) or 'none'} |")
        # Snippet block (ready-to-paste fix) with fence length that exceeds any backtick run inside snippet
        snippet_text = f.get("snippet", "")
        if snippet_text:
            fence_str = _fence(snippet_text)
            o.append(f"\n**Snippet:**\n\n{fence_str}\n{snippet_text}\n{fence_str}\n")
        agent_text = f.get("agent_prompt", "")
        if agent_text:
            fence_str2 = _fence(agent_text)
            o.append(f"**Agent prompt:**\n\n{fence_str2}\n{agent_text}\n{fence_str2}\n")
        o.append("")
    # Combined agent prompt (finding titles and fixes only, no repeated evidence or snippets)
    combined_fix_lines = []
    for f in r.get("findings", []):
        fix_text = f.get("fix", "").splitlines()[0]
        combined_fix_lines.append(f"- {f.get('title', '')} ({f.get('sev', '').upper()}): {fix_text}")
    combined_prompt = "Inspect findings and apply fixes:\n" + "\n".join(combined_fix_lines) if combined_fix_lines else "No findings to fix."
    # Keep combined prompt in JSON/HTML if needed; don't render as markdown block here (per-finding agent prompts remain in JSON)
    # Insert as a short section instead of whole-plan snippets
    o += ["## Combined agent prompt\n", f"{combined_prompt}\n"]
    o.append("## What this audit did not check\n")
    o.append("- Backlinks / referring domains\n")
    o.append("- Search Console (Google / Bing) data\n")
    o.append("- Core Web Vitals (LCP, INP, CLS)\n")
    o.append("- Server logs or user-agent filtering beyond Fetch failures\n")
    o.append("- WAF / bot-blocking rules beyond what Fetch reported\n")
    o.append("- Full index coverage or ranking history across other search engines\n")
    o.append("\n")
    o.append("## How this audit was produced\n")
    o.append("Every request below went to the live page or live search index (Fetch used `ttl=0`).\n")
    for kind, what in r["calls"]:
        o.append(f"- TinyFish {kind}: `{what[:110]}`")
    o.append("- Plain HTTP GET (not TinyFish): raw HTML for JSON-LD, meta description and the JavaScript-dependence check.")
    return "\n".join(o) + "\n"

def to_html(r: dict) -> str:
    P = r.get("page", {})
    S = r.get("scores", {})
    findings = r.get("findings", [])
    fix_first = r.get("fix_first", [])
    cat_scores = r.get("category_scores", {})
    visibility = r.get("visibility", {})
    parity = r.get("parity", {})
    bp = r.get("best_passage")
    comps = r.get("competitors", [])
    calls = r.get("calls", [])
    query = r.get("query", "")
    url = r.get("final_url") or r.get("url", "")
    checked = r.get("checked_at", "")
    rank = r.get("rank")
    serp_size = r.get("serp_size", 0)
    note_line = f"<p><em>{e(r.get('url_note'))}</em></p>" if r.get("url_note") else ""
    def sec(title, body): return f"<section><h2>{e(title)}</h2>\n{body}\n</section>"
    # Verdict
    score_rows = ""
    for area, s in (cat_scores or {}).items():
        score_rows += f"<tr><td>{e(area)}</td><td>{e(s)}</td><td>{e(sum(1 for f in findings if f.get('area')==area))}</td></tr>\n"
    verdict_body = (
        f"<ul>"
        f"<li><strong>AI readability:</strong> {e(S.get('ai_readability')) if S.get('ai_readability') is not None else 'n/a'}/100</li>"
        f"<li><strong>Search visibility:</strong> {e('position '+str(rank) if rank else 'not returned in top '+str(serp_size))} (score {e(S.get('search_visibility')) if S.get('search_visibility') is not None else 'n/a'}/100)</li>"
        f"<li><strong>Findings:</strong> {sum(1 for f in findings if f.get('sev')=='high')} high, {sum(1 for f in findings if f.get('sev')=='med')} medium, {sum(1 for f in findings if f.get('sev')=='low')} low</li>"
        f"</ul>"
        f"<table><thead><tr><th>Category</th><th>Score</th><th>Findings</th></tr></thead><tbody>{score_rows}</tbody></table>"
    )
    # Fix first
    def fix_for_title(title):
        f_map = {f.get('title'): f for f in findings}
        f_obj = f_map.get(title)
        if not f_obj:
            return ""
        fix_text = f_obj.get('fix','')
        return (fix_text.split('. ')[0] + '.') if '.' in fix_text else fix_text.splitlines()[0]
    ff_body = "<p>Nothing urgent.</p>" if not fix_first else "<ul>" + "".join(
        f"<li><strong>{e(title)}</strong>: {e(fix_for_title(title))}</li>"
        for title in fix_first
    ) + "</ul>"
    # Search view
    target = visibility.get("target", {}); topic = visibility.get("topic", {}); brand = visibility.get("brand", {})
    search_view = (
        f"<h3>Target</h3><p>Query: <code>{e(query)}</code> | Rank: {e(target.get('rank')) if target.get('rank') is not None else 'not returned'}</p>"
        f"<h3>Topic</h3><p>Rank: {e(topic.get('rank')) if topic.get('rank') is not None else 'not returned'}</p>"
        f"<h3>Brand</h3><p>Rank: {e(brand.get('rank')) if brand.get('rank') is not None else 'not returned'}</p>"
    )
    # Best passage
    bp_body = "<p>No best passage.</p>" if not bp else f"<blockquote>{e(bp.get('passage') or '')[:400]}</blockquote><p>Coverage: <strong>{int((bp.get('coverage',0) or 0)*100)}%</strong> | Offset: {e(bp.get('offset_words','n/a'))} | Length: {e(bp.get('length_words','n/a'))}</p>"
    # Competitor table
    comp_table = ""
    if comps:
        headers = ["Metric"] + [f"#{c.get('rank')}" for c in comps]
        comp_table += "<table><thead><tr>" + "".join(f"<th>{e(h)}</th>" for h in headers) + "</tr></thead><tbody>"
        for label, key, fmt in (
            ("Query terms in title","t_title",lambda v: f"{round(v*100)}%" if v is not None else "n/a"),
            ("Query terms in H1","t_h1",lambda v: f"{round(v*100)}%" if v is not None else "n/a"),
            ("Query terms in first 120 words","t_first",lambda v: f"{round(v*100)}%" if v is not None else "n/a"),
            ("Words","words",lambda v: str(v) if v is not None else "n/a"),
            ("H2 sections","h2",lambda v: str(v) if v is not None else "n/a"),
            ("Tables","tables",lambda v: str(v) if v is not None else "n/a"),
            ("Lists","lists",lambda v: str(v) if v is not None else "n/a"),
        ):
            comp_table += "<tr>" + f"<td>{e(label)}</td>" + f"<td>{e(fmt(P.get(key,'n/a')))}</td>" + "".join(f"<td>{e(fmt(c.get(key,'n/a')))}</td>" for c in comps) + "</tr>"
        comp_table += "</tbody></table>"
    else:
        comp_table = "<p>No competitor data.</p>"
    # Findings with collapsible details
    findings_body = ""
    for f in findings:
        details = f"<p><strong>Evidence:</strong> {e(f.get('evidence',''))}</p>"
        if f.get('fix'):
            details += f"<p><strong>Fix:</strong> {e(f.get('fix','').splitlines()[0])}</p>"
        if f.get('snippet'):
            details += f"<pre><code>{e(f.get('snippet',''))}</code></pre>"
        if f.get('agent_prompt'):
            details += f"<pre><code>{e(f.get('agent_prompt',''))}</code></pre>"
        findings_body += f"<details><summary><strong>[{e(f.get('sev',''))}] {e(f.get('title',''))}</strong> — {e(f.get('area',''))} · affects {e(f.get('affects',''))}</summary>{details}</details>"
    # Calls
    calls_body = "<ul>" + "".join(f"<li><code>{e(k)}: {e(w)}</code></li>" for k,w in calls) + "</ul>" if calls else "<p>No calls.</p>"
    # Not checked
    not_checked_items = [
        "Backlinks / referring domains","Search Console (Google / Bing) data",
        "Core Web Vitals (LCP, INP, CLS)","Server logs or user-agent filtering beyond Fetch failures",
        "WAF / bot-blocking rules beyond what Fetch reported",
        "Full index coverage or ranking history across other search engines",
    ]
    not_checked = "<ul>" + "".join(f"<li>{e(i)}</li>" for i in not_checked_items) + "</ul>"
    css_block = "<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.6;max-width:900px;margin:2rem auto;padding:0 1rem;color:#1a1a1a;background:#fff;}h1,h2,h3{color:#0b1a2e;border-bottom:1px solid #e2e8f0;padding-bottom:.25rem;}table{border-collapse:collapse;width:100%;margin:1rem 0;}th,td{border:1px solid #cbd5e1;padding:.5rem .75rem;text-align:left;}th{background:#f1f5f9;}code,pre{background:#f8fafc;border:1px solid #e2e8f0;padding:.15rem .35rem;border-radius:.25rem;font-family:ui-monospace,SFMono-Regular,monospace;}pre{padding:.75rem;overflow-x:auto;}blockquote{margin:1rem 0;padding:.75rem 1rem;background:#f8fafc;border-left:4px solid #94a3b8;}details{margin:.75rem 0;border:1px solid #e2e8f0;border-radius:.4rem;padding:.75rem;background:#f8fafc;}summary{cursor:pointer;font-weight:600;}@media print{body{max-width:100%;margin:0;}details{border:1px solid #ccc;}}</style>"
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SEO Audit — {e(query or url)}</title>{css_block}</head><body><h1>AI-readability &amp; search audit</h1><p><strong>Page:</strong> <a href="{e(url)}">{e(url)}</a></p><p><strong>Checked live:</strong> {e(checked)}</p><p><strong>Query:</strong> <code>{e(query or '')}</code></p><p><strong>Page title:</strong> <code>{e(P.get('title',''))}</code></p><p><strong>Excerpt:</strong> <code>{e(P.get('excerpt','')[:200])}</code></p>{note_line}{sec('Verdict and category scores', verdict_body)}{sec('Fix first', ff_body)}{sec('Search view (target/topic/brand)', search_view)}

{sec('Parity table', '<table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>' + ''.join(f'<tr><td>{e(k)}</td><td>{e(str(parity.get(k,"n/a")))}</td></tr>' for k in parity) + '</tbody></table>')}{sec('Best passage', bp_body)}{sec('Competitor table', comp_table)}{sec('Full findings', findings_body)}{sec('How this audit was produced (calls log)', calls_body)}{sec('What this audit did not check', not_checked)}</body></html>"""


BATCH_DELAY = 3.0


def safe_slug(host: str, path: str) -> str:
    combined = (host + (path or "")).lower()
    safe = re.sub(r"[^a-z0-9_.-]", "-", combined)
    safe = re.sub(r"-+", "-", safe)
    safe = safe.strip("-_.")
    safe = safe[:200]
    if not safe:
        safe = "page"
    reserved = {"con", "prn", "aux", "nul", "com1", "lpt1", "clock$", "nul"}
    base = safe.split(".")[0] if "." in safe else safe
    if base in reserved:
        safe = base + "_"
    for ch in ':?<>"|\\*':
        safe = safe.replace(ch, "-")
    safe = re.sub(r"[-_]+", "-", safe).strip("-")
    return safe[:200] or "page"


def parse_batch_lines(text: str):
    lines = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "," in line:
            url_part, query_part = line.split(",", 1)
        else:
            url_part, query_part = line, ""
        url_part = url_part.strip()
        query_part = query_part.strip()
        if url_part:
            lines.append((url_part, query_part or None))
    return lines


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
    # Exit codes: 0 complete observation, 1 input/config error, 2 partial or unavailable (page unreadable, search failed)
    ap = argparse.ArgumentParser(description=__doc__ + "\n\nExit codes: 0 complete observation, 1 input/config error, 2 partial/unavailable (page unreadable, search failed).", formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", help="URL to audit")
    ap.add_argument("query_words", nargs="*", help="target search query words (if -q is omitted)")
    ap.add_argument("-q", "--query", help="target search query (derived from the page if omitted)")
    ap.add_argument("-o", "--out", default="report", help="output prefix -> <out>.md and <out>.json")
    ap.add_argument("--country", help="country code for geo-targeted search, e.g. IN, US")
    ap.add_argument("--language", help="language code for result language, e.g. en, fr, de")
    ap.add_argument("--no-raw", action="store_true", help="skip the plain HTTP GET (no JSON-LD / JS-dependence checks)")
    ap.add_argument("--batch", metavar="FILE", help="batch file with url[,query] lines; writes samples/<slug>.md + .json and samples/index.md")
    ap.add_argument("--batch-delay", type=float, default=BATCH_DELAY, help="seconds between batch audits")
    ap.add_argument("--timeout", type=int, default=30, help="Request timeout in seconds")
    ap.add_argument("--retries", type=int, default=RETRY_MAX_ATTEMPTS, help="Max retry attempts for API calls")
    ap.add_argument("--html", action="store_true", help="also write a self-contained <out>.html report")
    a = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    load_dotenv()
    key = os.environ.get("TINYFISH_API_KEY")
    if not key:
        sys.stderr.write(redact("Set TINYFISH_API_KEY (free at agent.tinyfish.ai/api-keys)."))
        sys.exit(1)

    if a.batch:
        if not os.path.isfile(a.batch):
            sys.stderr.write(redact(f"Batch file not found: {a.batch}\n"))
            sys.exit(1)
        with open(a.batch, "r", encoding="utf-8") as f:
            batch_text = f.read()
        entries = parse_batch_lines(batch_text)
        if not entries:
            sys.stderr.write(redact("Batch file contained no valid URL lines.\n"))
            sys.exit(1)
        tf = TinyFish(key, timeout=a.timeout, max_retries=a.retries)
        results = []
        for idx, (raw_url, query) in enumerate(entries, 1):
            try:
                url, note = normalize_target(raw_url)
            except ValueError as ex:
                msg = redact(str(ex))
                sys.stderr.write(redact(f"Batch item {idx}: {msg}\n"))
                results.append({"slug": "invalid", "url": raw_url, "query": query, "status": "invalid_url", "note": str(ex)})
                if idx < len(entries):
                    time.sleep(a.batch_delay)
                continue
            try:
                res = audit(url, query, tf, do_raw=not a.no_raw, country=a.country, language=a.language, note=note)
            except Exception as ex:
                msg = redact(str(ex).splitlines()[0])
                sys.stderr.write(redact(f"Batch item {idx}: audit error: {msg}\n"))
                res = {"status": "error", "url": url, "url_note": note, "findings": [{"title": msg}], "query": query or ""}
            p = urlparse(res.get("final_url") or url)
            host = p.netloc.lower()
            path = p.path or "/"
            if urlparse(res.get("final_url") or url).query:
                path += "?" + urlparse(res.get("final_url") or url).query
            slug = safe_slug(host, path)
            os.makedirs("samples", exist_ok=True)
            md_path = os.path.join("samples", f"{slug}.md")
            json_path = os.path.join("samples", f"{slug}.json")
            # Never overwrite existing reports
            def safe_path(path, suffix=""):
                if not os.path.exists(path):
                    return path
                ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
                base, ext = os.path.splitext(path)
                return base + "_" + ts + (suffix or ext)
            md_path_final = safe_path(md_path)
            json_path_final = safe_path(json_path)
            with open(md_path_final, "w", encoding="utf-8") as f:
                f.write(redact(to_markdown(res)))
            with open(json_path_final, "w", encoding="utf-8") as f:
                f.write(json.dumps(redact_value(res), indent=2, default=str))
            results.append({"slug": slug, "url": res.get("final_url") or url, "query": res.get("query") or query, "ai_readability": res.get("scores", {}).get("ai_readability"), "search_visibility": res.get("scores", {}).get("search_visibility"), "high": sum(1 for f in res.get("findings", []) if f.get("sev") == "high"), "med": sum(1 for f in res.get("findings", []) if f.get("sev") == "med"), "low": sum(1 for f in res.get("findings", []) if f.get("sev") == "low"), "status": res.get("status")})
            if idx < len(entries):
                time.sleep(a.batch_delay)
        # Write index.md
        index_lines = ["# Batch audit index\n", f"Run date: {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC — audited live\n", ""]
        index_lines.append("| Page | Query | AI readability | Search visibility | High / Med / Low | Report |")
        index_lines.append("|---|---|---|---|---|---|")
        for r in results:
            page_name = r.get("url") or r.get("url", "")
            query_name = r.get("query") or ""
            ai = r.get("ai_readability")
            vis = r.get("search_visibility")
            ai_str = str(ai) if ai is not None else "—"
            vis_str = str(vis) if vis is not None else "—"
            counts = f"{r.get('high', 0)} / {r.get('med', 0)} / {r.get('low', 0)}"
            link = f"[{r['slug']}.md](samples/{r['slug']}.md)"
            index_lines.append(f"| {page_name} | {query_name} | {ai_str} | {vis_str} | {counts} | {link} |")
        with open(os.path.join("samples", "index.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(index_lines) + "\n")
        # Summary print
        ok = sum(1 for r in results if r.get("status") not in ("error", "invalid_url"))
        print(f"Batch complete: {ok}/{len(entries)} audited. Reports in samples/; index at samples/index.md")
        return

    raw_url = a.url
    interactive = False
    if not raw_url:
        interactive = True
        try:
            raw_url = input("URL: ").strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit(2)
        if not raw_url:
            sys.stderr.write("Error: URL is required.\n")
            sys.exit(2)

    query = a.query
    if not query:
        if a.query_words:
            query = " ".join(a.query_words)
        elif interactive:
            try:
                raw_q = input("Target query (optional): ").strip()
                query = raw_q or None
            except EOFError:
                query = None
            except KeyboardInterrupt:
                sys.exit(2)

    try:
        url, note = normalize_target(raw_url)
    except ValueError as ex:
        msg = redact(str(ex))
        sys.stderr.write(redact(f"Error: {msg}\n"))
        sys.exit(1)

    try:
        res = audit(url, query, TinyFish(key, timeout=a.timeout, max_retries=a.retries), do_raw=not a.no_raw, country=a.country, language=a.language, note=note)
    except (requests.RequestException, Exception) as ex:
        msg = redact(str(ex).splitlines()[0])
        sys.stderr.write(redact(f"Error: {msg} (Exit code 2)\n"))
        sys.exit(2)
    md_path = a.out + ".md"
    json_path = a.out + ".json"
    def safe_path(path):
        if not os.path.exists(path):
            return path
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
        base, ext = os.path.splitext(path)
        return base + "_" + ts + ext
    md_path_final = safe_path(md_path)
    json_path_final = safe_path(json_path)
    with open(md_path_final, "w", encoding="utf-8") as f:
        f.write(redact(to_markdown(res)))
    with open(json_path_final, "w", encoding="utf-8") as f:
        f.write(json.dumps(redact_value(res), indent=2, default=str))
    if a.html:
        html_path = a.out + ".html"
        html_path_final = safe_path(html_path)
        with open(html_path_final, "w", encoding="utf-8") as f:
            f.write(to_html(res))
        print(redact(f"wrote {html_path_final}"))
    final_path_md = md_path_final
    final_path_json = json_path_final
    if res.get("status") == "not_found":
        msg = redact(str(res['findings'][0]['title']))
        print(redact(f"NOT AUDITABLE: {msg} - {res['url']}"))
        print(redact(f"wrote {final_path_md} and {final_path_json}"))
        sys.exit(2)
    if res.get("status") == "error":
        msg = redact(str(res['findings'][0]['title']))
        print(redact(f"AUDIT ERROR: {msg} - {res['url']}"))
        print(redact(f"wrote {final_path_md} and {final_path_json}"))
        sys.exit(2)
    s = res["scores"]
    msg = redact(f"AI readability {s['ai_readability']}/100 | visibility {s['search_visibility']}/100 | rank {res['rank'] or 'not found'} for \"{res['query']}\"")
    print(msg)
    for f in res["findings"][:5]:
        print(redact(f"  [{f['sev']}] {f['title']}"))
    print(redact(f"wrote {final_path_md} and {final_path_json}"))
    return


if __name__ == "__main__":
    main()
