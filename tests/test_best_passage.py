import unittest
import ai_seo_auditor as A

class TestBestPassage(unittest.TestCase):
    def test_known_best_passage(self):
        md = "# Title\n\nThis is a short intro with no terms.\n\nThe best answer explains widgets and team workflow clearly for small companies looking for solutions.\n\nAnother paragraph with nothing."
        terms = ["widgets", "team"]
        res = A.best_passage(md, terms)
        self.assertIsNotNone(res)
        self.assertIn("widgets", res["passage"].lower())
        self.assertGreaterEqual(res["coverage"], 0.5)

    def test_deep_passage(self):
        filler = "Filler sentence number one. " * 60
        md = filler + "\n\nDeep content explains widgets and team dynamics in detail with many words about the topic and solutions.\n\nEnd."
        res = A.best_passage(md, ["widgets", "team"])
        self.assertIsNotNone(res)
        self.assertGreaterEqual(res["offset_words"], 0)

    def test_no_passage(self):
        md = "Some random text about nothing here at all with enough words to make a passage. " * 15
        res = A.best_passage(md, ["widgets"])
        self.assertIsNotNone(res)
        self.assertLess(res["coverage"], 0.5)

    def test_list_items(self):
        md = "- First item about widgets and team setup with enough words to reach the minimum length for analysis and scoring.\n- Second item\n- Third item with more details on widgets and team performance metrics."
        res = A.best_passage(md, ["widgets", "team"])
        self.assertIsNotNone(res)
        self.assertIn("widgets", res["passage"].lower())

if __name__ == "__main__":
    unittest.main()
