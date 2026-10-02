"""Offline tests. The fake client returns SYNTHETIC data shaped like real TinyFish responses.
They check the analysis logic and report wiring only; they are not a demo. Live check: run the CLI."""
import unittest
import os
import ai_seo_auditor as A

PAGE = "https://shop.example.com/blog/best-widgets/"
BODY = ("# Our Guide\n\n" + " ".join(f"Sentence number {i} talks about widgets and teams in ordinary words today." for i in range(40))
        + "\n\n## Pricing\n\nSome text here.\n\n#### Skipped level\n\nMore text.\n")
COMP_BODY = ("# Best widgets for small teams\n\n" + " ".join("Small teams need widgets that ship fast." for _ in range(90))
             + "\n\n## Comparison\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n## What is a widget?\n\n- one\n- two\n")


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


class Regressions(unittest.TestCase):
    """Bugs found by running the tool on real pages."""

    def test_acronym_matches_expansion(self):  # Wikipedia: query "seo" vs title "Search engine optimization"
        self.assertEqual(A.term_hits(["seo"], "Search engine optimization - Wikipedia"), 1)
        self.assertEqual(A.term_hits(["seo"], "Welcome to our shop"), 0)

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


class FullAudit(unittest.TestCase):
    def test_audit_finds_the_planted_problems(self):
        r = A.audit(PAGE, "best widgets for small teams", FakeTF(), raw_fn=fake_raw)
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


if __name__ == "__main__":
    unittest.main()
