import unittest
from unittest import mock
import ai_seo_auditor as A


class Regress(unittest.TestCase):
    def test_pilcrow_and_nbsp(self):
        self.assertEqual(A.clean("urllib.request \u2014 Extensible\u00b6"), "urllib.request \u2014 Extensible")
        self.assertEqual(A.clean("use the\u00a0web."), "use the web.")

    def test_snippet_tidy(self):
        s = "results\xa0... \u00b7 \u00b7"
        self.assertEqual(A.tidy_snippet(s), "results ...")

    def test_challenge(self):
        self.assertTrue(A.looks_like_challenge("Client Challenge", "", 36))
        self.assertTrue(A.looks_like_challenge("x", "JavaScript is disabled in your browser. Please enable JavaScript to proceed.", 36))
        self.assertFalse(A.looks_like_challenge("Real page", "we explain captcha " * 100, 2000))

    def test_best_h1(self):
        heads = [(1, "Tutorials"), (1, "Python Requests Module")]
        self.assertEqual(A.best_h1(heads, "Python Requests Module", ""), "Python Requests Module")
        self.assertEqual(A.best_h1([(1, "Everything AI needs"), (1, "Best Insurance 2026")], "Zzz", ""), "Everything AI needs")
        self.assertEqual(A.best_h1([], "t", "md h1"), "md h1")

    def test_first_skip(self):
        self.assertEqual(A.first_skip([(1, "Ops Copilot"), (3, "AI Call Scoring")]), ((1, "Ops Copilot"), (3, "AI Call Scoring")))
        self.assertIsNone(A.first_skip([(1, "a"), (2, "b"), (3, "c")]))

    def test_decode_utf8_without_charset(self):
        body = "a \u2014 b".encode("utf-8")
        r = mock.Mock(content=body, headers={"Content-Type": "text/html"}, text=body.decode("latin-1"))
        self.assertEqual(A.decode_body(r), "a \u2014 b")


class FakeTF:
    def __init__(self):
        self.calls = []

    def fetch(self, urls, **kw):
        self.calls.append(("fetch", ",".join(urls)))
        res = {}
        for u in urls:
            if "pypi" in u:
                res[u] = {"url": u, "text": "Client Challenge\n\nJavaScript is disabled in your browser. Please enable JavaScript to proceed.", "title": "Client Challenge"}
            elif kw.get("format") == "html":
                res[u] = {"url": u, "text": "<h1>Nav</h1><h1>Good Page Title</h1>"}
            else:
                res[u] = {"url": u, "text": "# Good Page Title\n\n" + "word " * 300, "title": "Good Page Title"}
        return res, {}

    def search(self, q, **kw):
        self.calls.append(("search", q))
        if kw.get("page", 0) > 0 or q.startswith("site:"):
            return []
        return [{"url": f"https://{h}/", "title": h, "snippet": "s"} for h in ("pypi.org", "a.com", "b.com", "c.com", "d.com")]


class Integration(unittest.TestCase):
    def test_challenge_competitor_skipped_and_backfilled(self):
        r = A.audit("https://me.example/", "good page", FakeTF(), do_raw=False)
        urls = [c["url"] for c in r["competitors"]]
        self.assertNotIn("https://pypi.org/", urls)
        self.assertEqual(len(urls), 3)
        self.assertTrue(any(e[2] == "bot_challenge_page" for e in r["competitor_errors"]))
        self.assertTrue(all(c["h1"] == "Good Page Title" for c in r["competitors"]))


if __name__ == "__main__":
    unittest.main()
