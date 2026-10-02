import unittest
import ai_seo_auditor as A

class TestFixFirst(unittest.TestCase):
    def test_selection_logic(self):
        # 1. Basic ordering and limit
        f_basic = [
            A.Finding("content", "low", "Low Sev", "ev", "Fix low", "search"),
            A.Finding("content", "high", "High Sev", "ev", "Fix high", "search"),
            A.Finding("content", "med", "Med Sev", "ev", "Fix med", "search"),
            A.Finding("content", "info", "Info Sev", "ev", "Fix info", "ai"),
        ]
        self.assertEqual(A.select_fix_first(f_basic), ["High Sev", "Med Sev", "Low Sev"])

        # 2. Tie-break: 'both' before 'search'
        f_tie = [
            A.Finding("content", "high", "High Search", "ev", "Fix s", "search"),
            A.Finding("content", "high", "High Both", "ev", "Fix b", "both"),
        ]
        self.assertEqual(A.select_fix_first(f_tie), ["High Both", "High Search"])

        # 3. Grouping: Title + H1
        f_group = [
            A.Finding("visibility", "high", "Query terms missing from your title tag", "ev", "Fix title", "search"),
            A.Finding("visibility", "high", "Query terms missing from your H1", "ev", "Fix h1", "search"),
            A.Finding("content", "med", "Other", "ev", "Fix other", "search"),
        ]
        res_group = A.select_fix_first(f_group)
        self.assertIn("Work the query terms into your title and H1", res_group)
        self.assertEqual(len(res_group), 2)

        # 4. Filtering: Optional/Info
        f_filter = [
            A.Finding("access", "low", "No llms.txt", "ev", "fix", "ai"),
            A.Finding("metadata", "low", "Meta description length", "ev", "fix", "search"),
            A.Finding("content", "high", "Urgent", "ev", "fix", "both"),
        ]
        self.assertEqual(A.select_fix_first(f_filter), ["Urgent"])

        # 5. Empty case
        self.assertEqual(A.select_fix_first([]), [])
        self.assertEqual(A.select_fix_first([A.Finding("a", "info", "No llms.txt", "e", "f", "ai")]), [])

    def test_markdown_rendering(self):
        r = {
            "final_url": "https://x.com/", "checked_at": "now", "query": "q", "query_source": "s",
            "rank": 1, "serp_entry": None, "serp_size": 10, "scores": {"ai_readability": 100, "search_visibility": 100},
            "page": {
                "words": 500, "title": "T", "h1": "H", "h2": 0, "tables": 0, "lists": 0,
                "description": "D", "description_source": "S", "canonical": "C", "robots": "R",
                "images": 0, "images_missing_alt": 0, "links": 0, "excerpt": "E",
                "jsonld_types": [], "raw_status": 200, "published": None, "author": None
            },
            "site": {"robots_txt": False, "llms_txt": False, "in_sitemap": True, "blocked_bots": []},
            "competitors": [],
            "competitor_errors": [],
            "findings": [
                {"title": "High Sev", "sev": "high", "fix": "Fix high. Second sentence.", "evidence": "ev", "area": "a", "affects": "b", "ties": ""},
                {"title": "Med Sev", "sev": "med", "fix": "Fix med", "evidence": "ev", "area": "a", "affects": "b", "ties": ""},
            ],
            "fix_first": ["High Sev", "Med Sev"],
            "calls": []
        }
        md = A.to_markdown(r)
        self.assertIn("## Fix first", md)
        self.assertIn("- **High Sev**: Fix high.", md)
        self.assertIn("- **Med Sev**: Fix med", md)

    def test_markdown_empty_fix_first(self):
        r = {
            "final_url": "https://x.com/", "checked_at": "now", "query": "q", "query_source": "s",
            "rank": 1, "serp_entry": None, "serp_size": 10, "scores": {"ai_readability": 100, "search_visibility": 100},
            "page": {
                "words": 500, "title": "T", "h1": "H", "h2": 0, "tables": 0, "lists": 0,
                "description": "D", "description_source": "S", "canonical": "C", "robots": "R",
                "images": 0, "images_missing_alt": 0, "links": 0, "excerpt": "E",
                "jsonld_types": [], "raw_status": 200, "published": None, "author": None
            },
            "site": {"robots_txt": False, "llms_txt": False, "in_sitemap": True, "blocked_bots": []},
            "competitors": [],
            "competitor_errors": [],
            "findings": [],
            "fix_first": [],
            "calls": []
        }
        md = A.to_markdown(r)
        self.assertIn("Nothing urgent. See the full list below.", md)

if __name__ == "__main__":
    unittest.main()
