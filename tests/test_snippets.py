import unittest, ai_seo_auditor as A

class SnippetTests(unittest.TestCase):
    def test_title_meta_snippet_assembled_from_h1(self):
        snippet = A.build_snippet_title_meta(
            h1="Our Guide to Best Widgets", title="Our Guide", query="best widgets",
            terms=["best", "widgets"], desc="A guide.", final_url="https://example.com/",
            meta={}, md_text="Our Guide to Best Widgets. Small teams need widgets that ship fast.")
        self.assertIn("Proposed title", snippet)
        self.assertIn("Proposed meta description", snippet)
        self.assertIn("Target URL", snippet)
        # Snippet no longer claims query terms appear unless explicitly checked
        self.assertIn("Assembled from page H1", snippet)

    def test_title_meta_snippet_says_terms_not_supported(self):
        snippet = A.build_snippet_title_meta(
            h1="Our Guide", title="Our Guide", query="unrelated term",
            terms=["unrelated"], desc="", final_url="https://example.com/",
            meta={}, md_text="Our Guide is here.")
        # Snippet no longer claims unsupported terms unless checked
        self.assertIn("Assembled from page H1", snippet)

    def test_canonical_snippet(self):
        snippet = A.build_snippet_canonical("https://example.com/page", {"canonical": "https://example.com/page"})
        self.assertIn('<link rel="canonical"', snippet)

    def test_og_twitter_omits_empty_tags(self):
        snippet = A.build_snippet_og_twitter("", "", {}, "https://example.com/", "")
        # With empty meta, snippet lists only missing tags (not a full proposal)
        self.assertIn("og:title", snippet)
        self.assertIn("twitter:card", snippet)
        self.assertNotIn("Assembled", snippet)

    def test_jsonld_article_with_date_author(self):
        snippet = A.build_snippet_jsonld(
            {"h1":"Post","title":"Post","published":"2026-01-01","author":"Alice","excerpt":"Hello"},
            "Hello", {"og":{"image":"/img.jpg"}}, "https://example.com/", True, ["{\"@type\":\"Article\"}"], "/post/")
        self.assertIn('"@type": "Article"', snippet)
        self.assertIn("datePublished", snippet)
        # Snippet explicitly says no placeholders; the old skeleton used it, but we removed it.
        self.assertNotIn("YYYY-MM-DD", snippet)
        self.assertNotIn("AUTHOR NAME", snippet)

    def test_jsonld_faqpage_from_evidence(self):
        snippet = A.build_snippet_jsonld(
            {"h1":"FAQ","title":"FAQ"}, "", {}, "https://example.com/faq", False,
            ["{\"@type\":\"FAQPage\",\"mainEntity\":[]}"], "/faq/")
        self.assertIn("FAQPage", snippet)
        self.assertIn("mainEntity", snippet)

    def test_jsonld_website_for_homepage(self):
        snippet = A.build_snippet_jsonld(
            {"h1":"","title":"Home"}, "", {}, "https://example.com/", False, [], "/")
        self.assertIn("WebPage", snippet)

    def test_robots_snippet_has_sitemap_and_optional_allow(self):
        snippet = A.build_snippet_robots("https://example.com", "", include_sitemap=True, include_bot_allow=True)
        self.assertIn("Sitemap:", snippet)
        self.assertIn("optional", snippet)

    def test_llms_snippet_from_links(self):
        # With fewer than 3 clean links, emit only header and summary (per requirement)
        snippet = A.build_snippet_llms("https://example.com", [{"url":"https://example.com/a","text":"About"}])
        self.assertIn("optional", snippet)
        # Should emit header and summary only when links are fewer than 3
        self.assertIn("header and summary", snippet)

    def test_backtick_safety_fence_expands(self):
        # Snippet containing triple backticks should not break out
        snippet = A.build_snippet_canonical("https://x.com", {}) + "\n```python\nprint(1)\n```"
        fence_str = A._fence(snippet)
        # Fence must be longer than any backtick run inside snippet
        self.assertGreater(len(fence_str), 3)
        self.assertIn("```python", snippet)  # original content preserved

    def test_agent_prompt_contains_all_parts(self):
        prompt = A.build_agent_prompt("https://example.com/", {"evidence":"ev","fix":"fx","verify":"v"}, "snippet")
        self.assertIn("Page URL:", prompt)
        self.assertIn("Evidence:", prompt)
        self.assertIn("Snippet", prompt)

if __name__ == "__main__":
    unittest.main()
