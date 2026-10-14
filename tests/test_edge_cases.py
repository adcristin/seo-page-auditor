"""Offline edge-case tests using a fake TinyFish client."""
import unittest
from ai_seo_auditor import audit, TinyFish, Finding


class FakeClient:
    """Minimal fake TinyFish client for offline tests."""
    def __init__(self, responses):
        # responses: dict of (kind, args) -> result; kind is 'fetch' or 'search'
        self.responses = responses
        self.calls = []

    def fetch(self, urls, **kwargs):
        self.calls.append(("fetch", ", ".join(urls)))
        key = ("fetch", tuple(urls))
        return self.responses.get(key, ({}, {}))

    def search(self, query, **params):
        self.calls.append(("search", query))
        return self.responses.get(("search", query), [])


class TestEdgeCases(unittest.TestCase):
    def test_scheme_added(self):
        tf = FakeClient({
            ("fetch", ("https://example.com",)): ({"https://example.com": {"text": "hello world", "title": "Hello", "final_url": "https://example.com"}}, {}),
            ("search", "example"): [{"url": "https://example.com"}],
        })
        r = audit("example.com", "example", tf, do_raw=False)
        self.assertTrue(r["url"].startswith("https://"))
        self.assertIsNotNone(r["scores"]["ai_readability"])

    def test_redirect_and_canonical_mismatch(self):
        tf = FakeClient({
            ("fetch", ("https://example.com/old",)): ({"https://example.com/old": {"text": "hello", "title": "T", "final_url": "https://example.com/new", "page_metadata": {"canonical": "https://example.com/other"}}}, {}),
            ("search", "q"): [],
        })
        r = audit("https://example.com/old", "q", tf, do_raw=False)
        self.assertNotEqual(r["url"], r["final_url"])
        titles = [f["title"] for f in r["findings"]]
        self.assertIn("Canonical points to a different URL", titles)

    def test_bot_blocked(self):
        tf = FakeClient({
            ("fetch", ("https://example.com/",)): ({}, {"https://example.com/": {"error": "bot_blocked", "status": 403}}),
        })
        r = audit("https://example.com/", "q", tf, do_raw=False)
        self.assertTrue(any("bot" in f["evidence"].lower() or "block" in f["title"].lower() for f in r["findings"]))
        self.assertIsNone(r["scores"]["ai_readability"])
        # Should not have readability findings pretending to measure content
        read_findings = [f for f in r["findings"] if f["area"] == "content"]
        self.assertEqual(len(read_findings), 0)

    def test_404(self):
        tf = FakeClient({
            ("fetch", ("https://example.com/missing",)): ({}, {"https://example.com/missing": {"error": "page_not_found", "status": 404}}),
            ("search", "site:example.com missing"): [{"url": "https://example.com/home"}],
        })
        r = audit("https://example.com/missing", "missing", tf, do_raw=False)
        self.assertEqual(r["status"], "not_found")
        self.assertTrue(any("does not exist" in f["title"] for f in r["findings"]))
        self.assertIsNone(r["scores"]["ai_readability"])

    def test_challenge_page(self):
        tf = FakeClient({
            ("fetch", ("https://example.com/",)): ({"https://example.com/": {"text": "just a moment... checking your browser to verify access", "title": "Just a moment...", "final_url": "https://example.com/"}}, {}),
            ("search", "q"): [],
        })
        r = audit("https://example.com/", "q", tf, do_raw=False)
        self.assertTrue(any("challenge" in f["title"].lower() for f in r["findings"]))

    def test_large_page(self):
        big_text = "word " * 200000
        tf = FakeClient({
            ("fetch", ("https://example.com/big",)): ({"https://example.com/big": {"text": big_text, "title": "Big", "final_url": "https://example.com/big"}}, {}),
            ("search", "q"): [],
        })
        r = audit("https://example.com/big", "q", tf, do_raw=False)
        self.assertIsNotNone(r["findings"])
        excerpt = r["page"]["excerpt"]
        self.assertLessEqual(len(excerpt), 500)

    def test_derived_query(self):
        tf = FakeClient({
            ("fetch", ("https://example.com/",)): ({"https://example.com/": {"text": "hello world", "title": "Hello World", "final_url": "https://example.com/"}}, {}),
            ("search", "hello world"): [{"url": "https://example.com/"}],
        })
        r = audit("https://example.com/", None, tf, do_raw=False)
        self.assertEqual(r["query_source"], "derived from the page's own H1/title (tests findability by name only; pass -q for a real keyword test)")

    def test_search_empty(self):
        tf = FakeClient({
            ("fetch", ("https://example.com/",)): ({"https://example.com/": {"text": "hello", "title": "Hello", "final_url": "https://example.com/"}}, {}),
            ("search", "unknownquery"): [],
        })
        r = audit("https://example.com/", "unknownquery", tf, do_raw=False)
        self.assertIsNone(r["rank"])
        self.assertIsNotNone(r["findings"])

    def test_competitor_fail_and_challenge_skipped(self):
        tf = FakeClient({
            ("fetch", ("https://example.com/",)): ({"https://example.com/": {"text": "my content here", "title": "My Page", "final_url": "https://example.com/", "page_metadata": {}}}, {}),
            ("search", "q"): [
                {"url": "https://example.com/", "title": "My Page"},
                {"url": "https://bad.com/", "title": "Bad"},
                {"url": "https://challenge.com/", "title": "Just a moment..."},
            ],
            ("fetch", ("https://bad.com/",)): ({}, {"https://bad.com/": {"error": "fetch_failed"}}),
            ("fetch", ("https://challenge.com/",)): ({"https://challenge.com/": {"text": "just a moment... verify you are human", "title": "Just a moment...", "final_url": "https://challenge.com/"}}, {}),
        })
        r = audit("https://example.com/", "q", tf, do_raw=False)
        errors = r.get("competitor_errors", [])
        self.assertTrue(any("bad.com" in str(e) for e in errors))
        # Challenge competitor should not appear in comps
        comp_urls = [c["url"] for c in r["competitors"]]
        self.assertNotIn("https://challenge.com/", comp_urls)


if __name__ == "__main__":
    unittest.main()
