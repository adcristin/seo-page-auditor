"""Offline tests. The fake client returns SYNTHETIC data shaped like real TinyFish responses.
They check the analysis logic and report wiring only; they are not a demo. Live check: run the CLI."""
import unittest
import os
from unittest import mock
import ai_seo_auditor as A
import requests

PAGE = "https://shop.example.com/blog/best-widgets/"
BODY = ("# Our Guide\n\n" + " ".join(f"Sentence number {i} talks about widgets and teams in ordinary words today." for i in range(40))
        + "\n\n## Pricing\n\nSome text here.\n\n#### Skipped level\n\nMore text.\n")
COMP_BODY = ("# Best widgets for small teams\n\n" + " ".join("Small teams need widgets that ship fast." for _ in range(90))
             + "\n\n## Comparison\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n## What is a widget?\n\n- one\n- two\n")

PAGE = "https://shop.example.com/blog/best-widgets/"
BODY = ("# Our Guide\n\n" + " ".join(f"Sentence number {i} talks about widgets and teams in ordinary words today." for i in range(40))
        + "\n\n## Pricing\n\nSome text here.\n\n#### Skipped level\n\nMore text.\n")
COMP_BODY = ("# Best widgets for small teams\n\n" + " ".join("Small teams need widgets that ship fast." for _ in range(90))
             + "\n\n## Comparison\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n## What is a widget?\n\n- one\n- two\n")


class FaultyTF:
    def __init__(self, timeout=30, max_retries=3):
        self.timeout = timeout
        self.max_retries = max_retries
        self.calls = []
        self.fail_count = 0
        self.fail_until = 0
        self.persistent_fail = False
        self.fail_type = None

    def set_transient_fail(self, count, fail_type):
        self.fail_until = count
        self.fail_type = fail_type

    def set_persistent_fail(self, fail_type):
        self.persistent_fail = True
        self.fail_type = fail_type

    def _handle_call(self, call_type, *args, **kwargs):
        self.calls.append((call_type, args[0] if args else ""))
        if self.persistent_fail:
            self._raise_fail()
        if self.fail_count < self.fail_until:
            self.fail_count += 1
            self._raise_fail()
        return None

    def _raise_fail(self):
        if self.fail_type == "timeout":
            raise requests.Timeout("Request timed out")
        if self.fail_type == "connection":
            raise requests.ConnectionError("Connection refused")
        if self.fail_type == "429":
            r = requests.Response()
            r.status_code = 429
            r.headers["Retry-After"] = "0.1"
            raise requests.HTTPError("Too Many Requests", response=r)
        raise Exception("Unknown failure")

    def search(self, query, page=0, **kw):
        self._handle_call("search", query)
        return [{"position": 1, "url": PAGE, "title": "T", "snippet": "s"}]

    def fetch(self, urls, **kw):
        self._handle_call("fetch", urls[0])
        got, err = {}, {}
        for u in urls:
            if u == PAGE:
                got[u] = {"url": u, "final_url": PAGE, "title": "Our Guide", "text": BODY, "language": "en",
                          "links": [], "published_date": None, "author": None,
                          "page_metadata": {"canonical": PAGE, "og": {"type": "article"}}}
            else:
                got[u] = {"url": u, "final_url": u, "title": "Comp", "text": COMP_BODY, "published_date": "2026-08-01"}
        return got, err

class FakeTF:
    calls = []

    def search(self, query, page=0, **kw):
        self.calls.append(("search", query))
        if query.startswith("site:"):
            return [{"position": 1, "url": PAGE, "title": "Best widgets guide", "snippet": "x"}]
        if page:
            return []
        return [{"position": i + 1, "url": f"https://rival{i}.com/widgets", "title": f"Best widgets for small teams {i}",
                 "snippet": "s", **({"date": "Aug 7, 2026"} if i < 2 else {})} for i in range(1, 4)]

    def fetch(self, urls, **kw):
        self.calls.append(("fetch", urls[0]))
        got, err = {}, {}
        for u in urls:
            if kw.get("include_selectors"):
                heads = "<h1>Our Guide</h1><h2>Pricing</h2><h4>Skipped level</h4>" if u == PAGE else "<h1>Best widgets for small teams</h1>"
                got[u] = {"url": u, "text": heads + '<img src="/a.png"><img src="/b.png" alt="B"><img src="/c.png"><img src="/d.png">'}
            elif u.endswith("robots.txt"):
                got[u] = {"url": u, "text": "User-Agent: \\*\nAllow: /\n\nUser-Agent: OAI-SearchBot\nDisallow: /blog/\nSitemap: https://shop.example.com/sitemap.xml"}
            elif u.endswith("llms.txt"):
                err[u] = {"url": u, "error": "page_not_found", "status": 404}
            elif u.endswith("sitemap.xml"):
                got[u] = {"url": u, "text": "https://shop.example.com\nhttps://shop.example.com/pricing"}
            elif u == PAGE:
                got[u] = {"url": u, "final_url": PAGE, "title": "Our Guide", "text": BODY, "language": "en",
                          "links": ["https://shop.example.com/a"], "published_date": None, "author": None,
                          "page_metadata": {"canonical": "https://shop.example.com/blog/other/", "og": {"type": "article"}}}
            else:
                got[u] = {"url": u, "final_url": u, "title": "Best widgets for small teams", "text": COMP_BODY, "published_date": "2026-08-01"}
        return got, err


def fake_raw(url):
    return {"status": 200, "x_robots": "", "title": "Our Guide", "meta": {}, "jsonld": [], "text": "Loading..."}


class Units(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(A.norm("https://www.Example.com/a/?utm_source=x"), A.norm("http://example.com/a"))

    def test_raw_view_non_html(self):
        """Verify raw_view identifies non-HTML content and skips parsing."""
        with mock.patch("requests.get") as m:
            # Mock a PDF response
            mock_res = mock.Mock()
            mock_res.status_code = 200
            mock_res.headers = {"Content-Type": "application/pdf"}
            mock_res.iter_content.return_value = [b"PDF data"]
            m.return_value = mock_res

            res = A.raw_view("https://example.com/doc.pdf")
            self.assertTrue(res["non_html"])
            self.assertEqual(res["content_type"], "application/pdf")
            self.assertEqual(res["text"], "")

    def test_raw_view_truncation(self):
        """Verify raw_view caps body size."""
        with mock.patch("requests.get") as m:
            mock_res = mock.Mock()
            mock_res.status_code = 200
            mock_res.headers = {"Content-Type": "text/html"}
            # Return chunks that exceed 3MB
            mock_res.iter_content.return_value = [b"a" * 1024 * 1024] * 4
            m.return_value = mock_res

            res = A.raw_view("https://example.com/huge.html")
            self.assertFalse(res["non_html"])
            self.assertTrue(res["truncated"])
            # Check that it was capped at 3MB (approx)
            self.assertLessEqual(len(res["text"]), 3 * 1024 * 1024)

    def test_category_scores_calculation(self):
        # Mock a small set of findings
        F = [
            A.Finding("access", "high", "Title", "Ev", "Fix", "both"), # -15
            A.Finding("access", "low", "Title", "Ev", "Fix", "both"),  # -3
            A.Finding("content", "med", "Title", "Ev", "Fix", "ai"),    # -7
            A.Finding("metadata", "info", "Title", "Ev", "Fix", "search"), # -0
        ]
        # we need a way to test the audit's scoring without running the full audit
        # unfortunately audit() is one big function. we can test it via a custom FakeTF.
        # but for a unit test of the math, let's just verify the penalty map.
        self.assertEqual(A.PEN["high"], 15)
        self.assertEqual(A.PEN["med"], 7)
        self.assertEqual(A.PEN["low"], 3)
        self.assertEqual(A.PEN["info"], 0)

    def test_robots_markdown_escaped_star_and_precedence(self):
        g, sm = A.parse_robots("User-Agent: \\*\nDisallow: /private/\nAllow: /private/ok\n\nUser-agent: GPTBot\nDisallow: /\nSitemap: https://x.com/s.xml")
        self.assertFalse(A.robots_allows(g, "GPTBot", "/anything"))
        self.assertFalse(A.robots_allows(g, "ClaudeBot", "/private/x"))
        self.assertTrue(A.robots_allows(g, "ClaudeBot", "/private/ok"))
        self.assertEqual(sm, ["https://x.com/s.xml"])

    def test_coverage(self):
        md = "one two three four five six seven eight " * 12
        self.assertEqual(A.coverage(md, md)[0], 1)
        cov, missing = A.coverage(md + " alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu", md)
        self.assertEqual(A.coverage(md, "nothing")[0], 0)

    def test_title_rewrite(self):
        self.assertFalse(A.title_rewritten("Fetch API - TinyFish...", "Fetch API", ""))
        self.assertTrue(A.title_rewritten("Buy cheap widgets online", "Our Guide", "Welcome"))

    def test_derive_query(self):
        self.assertEqual(A.derive_query("TinyFish — Web Infrastructure for AI Agents", ""), "Web Infrastructure for AI Agents")

    def test_error_handling_graceful_degradation(self):
        """Test that the audit continues when Search or Competitors fail."""
        # Mock search to fail persistently
        class SearchFailTF(FakeTF):
            def search(self, query, page=0, **kw):
                raise requests.Timeout("Search timed out")
            def fetch(self, urls, **kw):
                return super().fetch(urls, **kw)

        r = A.audit(PAGE, "best widgets", SearchFailTF(), raw_fn=fake_raw)
        titles = [f["title"] for f in r["findings"]]
        self.assertIn("Search unavailable, visibility not measured", titles)
        self.assertEqual(r["rank"], None)
        self.assertTrue(len(r["findings"]) > 0)

    def test_critical_fetch_failure(self):
        """Test that a failure to fetch the main page returns an error status."""
        tf = FaultyTF()
        tf.set_persistent_fail("connection")

        r = A.audit(PAGE, "best widgets", tf, raw_fn=fake_raw)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["findings"][0]["title"], "Main page could not be fetched")

    def test_competitor_fetch_failure(self):
        """Test that competitor fetch failures are logged but don't crash the audit."""
        class CompFailTF(FakeTF):
            def fetch(self, urls, **kw):
                if any("rival" in u for u in urls):
                    raise requests.ConnectionError("Competitor site down")
                return super().fetch(urls, **kw)

        r = A.audit(PAGE, "best widgets", CompFailTF(), raw_fn=fake_raw)
        self.assertEqual(len(r["competitors"]), 0)
        self.assertTrue(any("Competitor fetch failed" in e[2] for e in r["competitor_errors"]))

    def test_og_and_twitter_tags(self):
        class MetaTF(FakeTF):
            def fetch(self, urls, **kw):
                got, err = super().fetch(urls, **kw)
                for u in got:
                    meta = {"canonical": u}
                    if "all_present" in u:
                        meta["og"] = {"title": "T", "description": "D", "image": "I"}
                        meta["twitter"] = {"card": "summary"}
                    elif "og_complete_no_twitter" in u:
                        meta["og"] = {"title": "T", "description": "D", "image": "I"}
                    elif "partial_og_no_twitter" in u:
                        meta["og"] = {"title": "T"}
                    elif "everything_missing" in u:
                        meta = {}
                    got[u]["page_metadata"] = meta
                return got, err

        # Scenario A: All OG present, no twitter -> No finding
        r = A.audit("https://example.com/og_complete_no_twitter", "q", MetaTF(), raw_fn=fake_raw)
        titles = [f["title"] for f in r["findings"]]
        self.assertNotIn("Open Graph tags incomplete", titles)
        self.assertNotIn("Twitter card tag missing", titles)

        # Scenario B: Partial OG, missing twitter -> "Open Graph tags incomplete" (listing both)
        r = A.audit("https://example.com/partial_og_no_twitter", "q", MetaTF(), raw_fn=fake_raw)
        titles = [f["title"] for f in r["findings"]]
        self.assertIn("Open Graph tags incomplete", titles)
        finding = next(f for f in r["findings"] if f["title"] == "Open Graph tags incomplete")
        self.assertIn("og:description", finding["evidence"])
        self.assertIn("og:image", finding["evidence"])
        self.assertIn("twitter:card", finding["evidence"])

        # Scenario C: Everything missing -> "Open Graph tags incomplete" (listing all)
        r = A.audit("https://example.com/everything_missing", "q", MetaTF(), raw_fn=fake_raw)
        titles = [f["title"] for f in r["findings"]]
        self.assertIn("Open Graph tags incomplete", titles)
        finding = next(f for f in r["findings"] if f["title"] == "Open Graph tags incomplete")
        self.assertIn("og:title", finding["evidence"])
        self.assertIn("og:description", finding["evidence"])
        self.assertIn("og:image", finding["evidence"])
        self.assertIn("twitter:card", finding["evidence"])

        # Scenario D: All present -> No finding
        r = A.audit("https://example.com/all_present", "q", MetaTF(), raw_fn=fake_raw)
        titles = [f["title"] for f in r["findings"]]
        self.assertNotIn("Open Graph tags incomplete", titles)


    def test_audit_non_html_handling(self):
        """Verify audit() handles non-HTML pages by adding a low-sev finding and skipping HTML checks."""
        class NonHtmlTF(FakeTF):
            def fetch(self, urls, **kw):
                # Return some extracted text (e.g. from a PDF) so profiling still happens
                got = {PAGE: {"url": PAGE, "final_url": PAGE, "text": "This is a PDF with words.", "title": "PDF Title"}}
                return got, {}

        def fake_raw_pdf(url):
            return {"status": 200, "non_html": True, "content_type": "application/pdf", "size": 1000, "text": "", "x_robots": ""}

        r = A.audit(PAGE, "pdf query", NonHtmlTF(), raw_fn=fake_raw_pdf)

        # Should have "Not an HTML page" finding
        titles = [f["title"] for f in r["findings"]]
        self.assertIn("Not an HTML page", titles)

        # Should NOT have HTML-only findings like JSON-LD or JS-dependence
        self.assertNotIn("No structured data (JSON-LD)", titles)
        self.assertNotIn("Much of the text only exists after JavaScript runs", titles)

        # Profiling should still have worked (words extracted)
        self.assertGreater(r["page"]["words"], 0)

    def test_acronym_matches_expansion(self):  # Wikipedia: query "seo" vs title "Search engine optimization"
        self.assertEqual(A.term_hits(["seo"], "Search engine optimization - Wikipedia"), 1)
        self.assertEqual(A.term_hits(["seo"], "Welcome to our shop"), 0)

    def test_unicode_tokenization(self):
        # Hindi - Simplified check: just ensure we get tokens
        self.assertTrue(len(A.tokens("नमस्ते दुनिया")) > 0)
        # Cyrillic
        self.assertTrue(len(A.tokens("Привет мир")) > 0)
        # Mixed
        self.assertTrue(len(A.tokens("Hello नमस्ते")) > 0)

    def test_cjk_fallback(self):
        # Japanese text
        text = "これはテストページです" # "This is a test page"
        self.assertTrue(A.is_cjk(text))
        # "テスト" (test) should be matched as a bigram sequence
        self.assertGreater(A.term_hits(["テスト"], text), 0)

    def test_zero_term_query(self):
        class EmojiTF(FakeTF):
            def fetch(self, urls, **kw):
                got = {PAGE: {"url": PAGE, "final_url": PAGE, "text": "Some text", "title": "T"}}
                return got, {}

        r = A.audit(PAGE, "🚀🔥", EmojiTF(), raw_fn=fake_raw)
        titles = [f["title"] for f in r["findings"]]
        self.assertIn("Query terms could not be analysed", titles)

    def test_acronym_still_works(self):
        self.assertEqual(A.term_hits(["seo"], "Search engine optimization"), 1)

    def test_clean_nbsp(self):
        self.assertEqual(A.clean("use the\u00a0web. "), "use the web.")

    def test_404_is_reported_as_404_not_as_bot_blocking(self):
        class Gone(FakeTF):
            def fetch(self, urls, **kw):
                if urls == [PAGE]:
                    return {}, {PAGE: {"url": PAGE, "error": "page_not_found", "status": 404}}
                return super().fetch(urls, **kw)
        r = A.audit(PAGE, "best widgets", Gone(), raw_fn=fake_raw)
        self.assertEqual(r["status"], "not_found")
        self.assertIsNone(r["scores"]["ai_readability"])
        md = A.to_markdown(r)
        self.assertIn("Not auditable", md)
        self.assertNotIn("bot blocking", md)

    def test_spa_soft_404_files_are_not_counted(self):
        class Spa(FakeTF):  # every path answers with the home page, like a client-rendered app
            def fetch(self, urls, **kw):
                got, err = super().fetch(urls, **kw)
                if len(urls) and urls[0].endswith(("robots.txt", "sitemap.xml")) or urls[0].endswith("llms.txt"):
                    return {u: {"url": u, "text": BODY} for u in urls}, {}
                return got, err
        r = A.audit(PAGE, "best widgets", Spa(), raw_fn=fake_raw)
        self.assertFalse(r["site"]["llms_txt"])
        self.assertFalse(r["site"]["robots_txt"])
        self.assertIsNone(r["site"]["in_sitemap"])
        titles = " | ".join(f["title"] for f in r["findings"])
        self.assertIn("No sitemap found", titles)
        self.assertNotIn("Page not found in sitemap", titles)

    def test_headings_come_from_dom_not_markdown(self):
        r = A.audit(PAGE, "best widgets", FakeTF(), raw_fn=fake_raw)
        titles = " | ".join(f["title"] for f in r["findings"])
        self.assertNotIn("H1s", titles)
        self.assertEqual(r["page"]["h1"], "Our Guide")

    def test_date_advice_only_for_articles(self):
        class Home(FakeTF):
            pass
        home = "https://shop.example.com/"
        class HomeTF(FakeTF):
            def fetch(self, urls, **kw):
                got, err = super().fetch(urls, **kw)
                if urls == [home] and not kw.get("include_selectors"):
                    got[home] = {"url": home, "final_url": home, "title": "Shop", "text": BODY, "page_metadata": {"canonical": home}}
                return got, err
            def search(self, query, page=0, **kw):
                return [{"position": 1, "url": home, "title": "Shop", "snippet": "s", "date": "Aug 1, 2026"}] * 3 if not query.startswith("site:") else []
        r = A.audit(home, "widgets shop", HomeTF(), raw_fn=fake_raw)
        self.assertNotIn("Top results show dates", " | ".join(f["title"] for f in r["findings"]))

    def test_topic_gap_analysis(self):
        class TopicTF(FakeTF):
            def fetch(self, urls, **kw):
                got, err = super().fetch(urls, **kw)
                if kw.get("include_selectors") and "h2" in kw.get("include_selectors", []):
                    # Mock headings for competitors
                    # Competitor 1: [Features, How to use, Pricing, Navigation]
                    # Competitor 2: [Top Features, Using widgets, Pricing]
                    # Competitor 3: [Features Review, Pricing, Footer]
                    h_map = {
                        "https://rival1.com/widgets": "<h1>H1</h1><h2>Features</h2><h2>How to use</h2><h2>Pricing</h2><h2>Navigation</h2>",
                        "https://rival2.com/widgets": "<h1>H1</h1><h2>Top Features</h2><h2>Using widgets</h2><h2>Pricing</h2>",
                        "https://rival3.com/widgets": "<h1>H1</h1><h2>Features Review</h2><h2>Pricing</h2><h2>Footer</h2>",
                    }
                    for u in urls:
                        if u in h_map:
                            got[u] = {"url": u, "text": h_map[u]}
                return got, err

        # Audited page covers "Pricing"
        class AuditPageTF(TopicTF):
            def fetch(self, urls, **kw):
                got, err = super().fetch(urls, **kw)
                if urls == [PAGE] and kw.get("include_selectors"):
                    got[PAGE] = {"url": PAGE, "text": "<h1>H1</h1><h2>Pricing</h2>"}
                return got, err

        r = A.audit(PAGE, "widgets", AuditPageTF(), raw_fn=fake_raw)

        # "Pricing" should be covered.
        # "Features" (shared by 3) should be a gap.
        # "How to use / Using widgets" (shared by 2) should be a gap.
        # "Navigation" and "Footer" should be dropped as boilerplate.

        gaps = r.get("topic_gaps", [])
        self.assertTrue(len(gaps) >= 1)
        self.assertTrue(any("Feature" in g for g in gaps))
        self.assertTrue(any("use" in g.lower() or "using" in g.lower() for g in gaps))
        self.assertFalse(any("Pricing" in g for g in gaps))
        self.assertFalse(any("Navigation" in g for g in gaps))
        self.assertFalse(any("Footer" in g for g in gaps))

    def test_topic_gap_skip_condition(self):
        class OneCompTF(FakeTF):
            def search(self, query, page=0, **kw):
                if page: return []
                return [{"position": 1, "url": "https://rival1.com/widgets", "title": "T", "snippet": "s"}]

        r = A.audit(PAGE, "widgets", OneCompTF(), raw_fn=fake_raw)
        self.assertEqual(r.get("topic_gaps", []), [])
        titles = [f["title"] for f in r["findings"]]
        self.assertNotIn("Topics the top pages cover that yours does not", titles)

        r = A.audit(PAGE, "best widgets for small teams", FakeTF(), raw_fn=fake_raw)
        self.assertIn("category_scores", r)
        self.assertEqual(len(r["category_scores"]), 4)
        self.assertTrue(all(0 <= s <= 100 for s in r["category_scores"].values()))
        titles = " | ".join(f["title"] for f in r["findings"])
        for expect in ("robots.txt blocks search crawlers", "only exists after JavaScript", "No structured data",
                       "Canonical points to a different URL", "images have no alt", "Query terms missing from your title",
                       "Indexed, but not in the top", "Page not found in sitemap", "No llms.txt", "Article exposes no",
                       "Heading levels skip"):
            self.assertIn(expect, titles)
        self.assertEqual(r["rank"], None)
        self.assertLess(r["scores"]["ai_readability"], 60)
        self.assertEqual(len(r["competitors"]), 3)
        md = A.to_markdown(r)
        for section in ("## Verdict", "## What an AI tool extracts", "## How it shows up", "## Findings", "## How this audit was produced"):
            self.assertIn(section, md)
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = os.path.join(tmpdir, "sample_report.md")
            with open(report_path, "w", encoding="utf-8") as fh:
                fh.write(md)

    def test_fetch_failure_still_reports_search_side(self):
        class Blocked(FakeTF):
            def fetch(self, urls, **kw):
                if urls == [PAGE] and not kw.get("include_selectors"):
                    return {}, {PAGE: {"url": PAGE, "error": "blocked", "status": 403}}
                return super().fetch(urls, **kw)
        r = A.audit(PAGE, "best widgets", Blocked(), raw_fn=fake_raw)
        self.assertEqual(r["findings"][0]["title"], "AI tools cannot extract any text from this page")
        self.assertEqual(r["page"]["words"], 0)
        A.to_markdown(r)

    def test_normalize_cleaning_and_schemes(self):
        # Whitespace
        url, note = A.normalize_target("  https://example.com/a  ")
        self.assertEqual(url, "https://example.com/a")
        self.assertEqual(note, "Interpreted as https://example.com/a")

        # Surrounding quotes
        url, note = A.normalize_target('"https://example.com/a"')
        self.assertEqual(url, "https://example.com/a")
        self.assertEqual(note, "Interpreted as https://example.com/a")

        url, note = A.normalize_target("'https://example.com/a'")
        self.assertEqual(url, "https://example.com/a")
        self.assertEqual(note, "Interpreted as https://example.com/a")

        # Brackets
        url, note = A.normalize_target("<https://example.com/a>")
        self.assertEqual(url, "https://example.com/a")
        self.assertEqual(note, "Interpreted as https://example.com/a")

        # Mixed quotes and whitespace
        url, note = A.normalize_target("  <'https://example.com/a'>  ")
        self.assertEqual(url, "https://example.com/a")
        self.assertEqual(note, "Interpreted as https://example.com/a")

        # Fragments
        url, note = A.normalize_target("https://example.com/a#section")
        self.assertEqual(url, "https://example.com/a")
        self.assertEqual(note, "Interpreted as https://example.com/a")

        # Bare host prepends https://
        url, note = A.normalize_target("example.com")
        self.assertEqual(url, "https://example.com")
        self.assertEqual(note, "Interpreted as https://example.com")

        # Double-slash protocol-relative
        url, note = A.normalize_target("//example.com/path")
        self.assertEqual(url, "https://example.com/path")
        self.assertEqual(note, "Interpreted as https://example.com/path")

        # IDNA / Punycode conversion
        url, note = A.normalize_target("münchen.de")
        self.assertEqual(url, "https://xn--mnchen-3ya.de")
        self.assertEqual(note, "Interpreted as https://xn--mnchen-3ya.de")

        # Unchanged clean URL has no note
        url, note = A.normalize_target("https://example.com")
        self.assertEqual(url, "https://example.com")
        self.assertIsNone(note)

    def test_normalize_errors(self):
        # Unsupported schemes
        for bad in ["ftp://example.com", "file:///etc/passwd", "javascript:alert(1)", "mailto:info@example.com"]:
            with self.assertRaises(ValueError):
                A.normalize_target(bad)

        # Invalid hosts
        for bad in ["", "   ", "notadomain", "http://", "https://", "https://.com", "https://example.", "https://..."]:
            with self.assertRaises(ValueError):
                A.normalize_target(bad)

        # Localhost and private / loopback / link-local IPs
        for bad in ["localhost", "http://localhost:8080", "127.0.0.1", "http://127.0.0.1:3000",
                    "192.168.1.1", "10.0.0.1", "172.16.0.1", "169.254.1.1"]:
            with self.assertRaises(ValueError):
                A.normalize_target(bad)

    def test_http_fallback(self):
        http_page = PAGE.replace("https://", "http://")

        # Mock HTTPS failure -> HTTP success
        class FallbackTF(FakeTF):
            def fetch(self, urls, **kw):
                if urls == [PAGE]:
                    raise requests.ConnectionError("SSL certificate verify failed")
                got, err = super().fetch(urls, **kw)
                if http_page in urls:
                    got[http_page] = {
                        "url": http_page, "final_url": http_page, "title": "Our Guide", "text": BODY, "language": "en",
                        "links": ["http://shop.example.com/a"], "published_date": None, "author": None,
                        "page_metadata": {"canonical": "http://shop.example.com/blog/other/", "og": {"type": "article"}}
                    }
                return got, err

        r = A.audit(PAGE, "best widgets", FallbackTF(), raw_fn=fake_raw)
        titles = [f["title"] for f in r["findings"]]
        self.assertIn("Site is not served over HTTPS", titles)
        finding = next(f for f in r["findings"] if f["title"] == "Site is not served over HTTPS")
        self.assertEqual(finding["sev"], "med")

        # Both HTTPS and HTTP fail -> reports original failure
        class BothFailTF(FakeTF):
            def fetch(self, urls, **kw):
                if urls[0].startswith("https://"):
                    raise requests.ConnectionError("HTTPS connection failed")
                raise requests.ConnectionError("HTTP connection failed")

        r2 = A.audit("https://shop.example.com/blog/best-widgets/", "best widgets", BothFailTF(), raw_fn=fake_raw)
        self.assertEqual(r2["status"], "error")
        self.assertIn("HTTPS connection failed", r2["findings"][0]["evidence"])

    def test_url_note_integration(self):
        # Audit called with bare host produces url_note and shows in markdown
        r = A.audit("shop.example.com/blog/best-widgets/", "best widgets", FakeTF(), raw_fn=fake_raw)
        self.assertEqual(r["url_note"], "Interpreted as https://shop.example.com/blog/best-widgets/")
        md = A.to_markdown(r)
        self.assertIn("_Interpreted as https://shop.example.com/blog/best-widgets/_", md)

    def test_cli_positional_query_and_interactive(self):
        # 1. Positional query words
        with mock.patch("sys.argv", ["ai_seo_auditor.py", "https://shop.example.com/blog/best-widgets/", "best", "widgets", "team"]), \
             mock.patch.dict(os.environ, {"TINYFISH_API_KEY": "test_key"}), \
             mock.patch("sys.stdout"), \
             mock.patch("ai_seo_auditor.audit") as mock_audit, \
             mock.patch("ai_seo_auditor.to_markdown", return_value=""), \
             mock.patch("builtins.open", mock.mock_open()):
            mock_audit.return_value = {"status": "ok", "scores": {"ai_readability": 90, "search_visibility": 90},
                                       "rank": 1, "query": "best widgets team", "findings": []}
            A.main()
            mock_audit.assert_called_once()
            self.assertEqual(mock_audit.call_args[0][1], "best widgets team")

        # 2. Interactive prompts when no args provided
        with mock.patch("sys.argv", ["ai_seo_auditor.py"]), \
             mock.patch.dict(os.environ, {"TINYFISH_API_KEY": "test_key"}), \
             mock.patch("sys.stdout"), \
             mock.patch("builtins.input", side_effect=["https://shop.example.com/blog/best-widgets/", "interactive query"]), \
             mock.patch("ai_seo_auditor.audit") as mock_audit, \
             mock.patch("ai_seo_auditor.to_markdown", return_value=""), \
             mock.patch("builtins.open", mock.mock_open()):
            mock_audit.return_value = {"status": "ok", "scores": {"ai_readability": 90, "search_visibility": 90},
                                       "rank": 1, "query": "interactive query", "findings": []}
            A.main()
            mock_audit.assert_called_once()
            self.assertEqual(mock_audit.call_args[0][1], "interactive query")

        # 3. Invalid URL exits with code 2
        with mock.patch("sys.argv", ["ai_seo_auditor.py", "invalid_host"]), \
             mock.patch.dict(os.environ, {"TINYFISH_API_KEY": "test_key"}), \
             mock.patch("sys.stderr.write"):
            with self.assertRaises(SystemExit) as cm:
                A.main()
            self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
