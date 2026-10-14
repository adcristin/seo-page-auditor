import unittest, json, os, ai_seo_auditor as A

BANNED = ["indexed", "not indexed", "deindexed", "suspect indexing", "ranking factor",
          "will rank", "hurts ranking", "cannot be cited", "AI tools cannot"]

class BannedPhraseTest(unittest.TestCase):
    def test_reference_fixtures_have_no_banned_phrase(self):
        fixtures_dir = "samples/regression"
        for name in os.listdir(fixtures_dir):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(fixtures_dir, name), "r", encoding="utf-8") as f:
                data = json.load(f)
            md = A.to_markdown(data)
            text = md.lower()
            for phrase in BANNED:
                self.assertNotIn(phrase, text, f"Banned phrase '{phrase}' found in {name}")

if __name__ == "__main__":
    unittest.main()
