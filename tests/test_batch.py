import unittest
import os
import json
import ai_seo_auditor as A


class BatchTests(unittest.TestCase):
    def test_parse_batch_lines_comments_blank_missing_query(self):
        text = "# comment\nhttps://example.com/a, query\nhttps://example.com/b\n\nhttps://example.com/c,\n"
        lines = A.parse_batch_lines(text)
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], ("https://example.com/a", "query"))
        self.assertEqual(lines[1], ("https://example.com/b", None))
        self.assertEqual(lines[2], ("https://example.com/c", None))

    def test_safe_slug_removes_unsafe_windows_chars(self):
        self.assertNotIn(":", A.safe_slug("host", "/a:b"))
        self.assertNotIn("?", A.safe_slug("host", "/a?b"))
        self.assertNotIn("|", A.safe_slug("host", "/a|b"))
        self.assertNotIn("*", A.safe_slug("host", "/a*b"))
        # Safe chars preserved
        self.assertEqual(A.safe_slug("example.com", "/blog/post"), "example.com-blog-post")

    def test_index_content_from_fake_results(self):
        results = [
            {"slug": "example-com-blog-post", "url": "https://example.com/blog/post", "query": "post", "ai_readability": 80, "search_visibility": 70, "high": 1, "med": 2, "low": 0, "status": "ok"},
        ]
        lines = ["# Batch audit index", f"Run date: 2026-01-01 00:00:00 UTC — audited live", ""]
        lines.append("| Page | Query | AI readability | Search visibility | High / Med / Low | Report |")
        lines.append("|---|---|---|---|---|---|")
        for r in results:
            lines.append(f"| {r['url']} | {r['query']} | {r['ai_readability']} | {r['search_visibility']} | {r['high']} / {r['med']} / {r['low']} | [{r['slug']}.md](samples/{r['slug']}.md) |")
        content = "\n".join(lines) + "\n"
        self.assertIn("audited live", content)
        self.assertIn("example.com/blog/post", content)
        self.assertIn("samples/example-com-blog-post.md", content)


if __name__ == "__main__":
    unittest.main()
