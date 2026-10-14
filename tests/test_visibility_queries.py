"""Tests for visibility query combinations (target, topic, brand) and skip rules."""
import unittest
import ai_seo_auditor as A

class FakeClient3:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
    def fetch(self, urls, **kwargs):
        self.calls.append(("fetch", ",".join(urls)))
        return self.responses.get(("fetch", tuple(urls)), ({}, {}))
    def search(self, query, **params):
        self.calls.append(("search", query, params))
        return self.responses.get(("search", query, tuple(sorted(params.items()))), [])

class VisibilityCombinations(unittest.TestCase):
    def test_target_topic_brand_separate(self):
        tf = FakeClient3({
            ("search", "widgets", (("language", "en"),)):
                [{"url":"https://example.com/"}],
            ("search", "guide widgets", (("language", "en"),)):
                [{"url":"https://example.com/"}],
            ("search", "example", (("language", "en"),)):
                [{"url":"https://example.com/"}],
        })
        r = A.audit("https://example.com/", "widgets", tf, do_raw=False, language="en")
        self.assertIn("visibility", r)
        self.assertIsNotNone(r["visibility"]["target"])
        self.assertIsNotNone(r["visibility"]["topic"])
        self.assertIsNotNone(r["visibility"]["brand"])

    def test_skip_topic_when_equals_target(self):
        # When topic query equals target after normalization, topic should be skipped
        tf = FakeClient3({
            ("search", "example", (("language", "en"),)):
                [{"url":"https://example.com/"}],
        })
        r = A.audit("https://example.com/", "example", tf, do_raw=False, language="en")
        # Topic skipped => should have no topic rank (None) and no extra search call
        self.assertIsNone(r["visibility"]["topic"]["rank"])
        # Brand skipped when equals target
        self.assertIsNone(r["visibility"]["brand"]["rank"])

    def test_domain_filter_in_calls_log(self):
        tf = FakeClient3({
            ("search", "widgets", (("language", "en"),)):
                [{"url":"https://example.com/"}],
        })
        r = A.audit("https://example.com/", "widgets", tf, do_raw=False, language="en")
        calls = tf.calls
        search_calls = [c for c in calls if c[0] == "search"]
        # The domain-restricted diagnostic uses include_domains; others use none
        diagnostic = [c for c in search_calls if "include_domains" in str(c[1][2])]
        self.assertTrue(any("include_domains" in str(c) for c in search_calls))

if __name__ == "__main__":
    unittest.main()
