# AI-readability and search-visibility auditor

This CLI audits a single public URL for AI-readability (what an AI fetcher can extract) and search visibility (how the page shows up for a query). It uses TinyFish Fetch and Search to observe a live page and its live results, compares the audited page with the top competitors, and writes a report with findings ordered by severity. It is intended for reviewers and maintainers who want accurate, verifiable observations rather than benchmarks or ranking guarantees.

## Quick start

Install the only dependency (`requests`), create `.env` with your key (never commit real keys), and run:

```bash
pip install requests
# .env file (add to .gitignore):
export TINYFISH_API_KEY=<your_key>

python ai_seo_auditor.py https://example.com/page -q "target query" -o report
python ai_seo_auditor.py --batch urls.csv --batch-delay 3 --country IN
python ai_seo_auditor.py https://example.com/page --no-raw -o report_no_raw
```

Single-run reports go to `<out>.md` and `<out>.json`; if the file exists, a UTC timestamp suffix is added. Batch writes `samples/<slug>.md` and `.json`, plus `samples/index.md`. Use `--country` for geo-targeted search and `--language` for language filtering. The `--no-raw` flag skips the plain HTTP GET; without it, the audit checks JSON-LD, meta description, and JavaScript dependence.

## How TinyFish is used

Every audit makes live calls. Fetch uses `ttl=0` (live, not cached). Search checks pages 0-2 for the target query, plus a `site:` fallback when the page is missing.

| Pipeline step | Endpoint | What it contributes |
|---|---|---|
| Fetch markdown (`format=markdown`, `page_metadata`, `links`, `ttl=0`) | **Fetch** | The extracted text, headings, tables, canonical, robots directives, OG tags, date/author that an AI tool receives after real browser rendering. |
| Fetch HTML (`format=html`, `include_selectors=["h1"-"h6","img"]`) | **Fetch** | Real `<h1>`-`<h6>` tags and image alt-text. Images without alt are invisible to extractors. |
| `/robots.txt`, `/llms.txt`, `/sitemap.xml` (+ child sitemaps) | **Fetch** | Which bots are allowed/disallowed on this path; sitemap membership; `llms.txt` presence. Fetch parses sitemap URLs into lists. |
| Target query, pages 0-2 | **Search** | The page's observed rank, title, and snippet for the query. |
| `site:<domain> <page title>` (only when page not found) | **Search** | Separates "indexed but weak" from "not indexed"; different fixes apply to each case. |
| Top 3 competing results | **Fetch** | The pages above you are read with the same Fetch call and scored on the same yardstick. This links readability to visibility: query terms in title/H1/opening text, word count, H2 sections, tables, lists, dates. |

The plain HTTP GET is not a TinyFish call. It reads the raw first response (max 3 MB, max 5 redirects, with SSRF protection) so the audit can see JSON-LD, meta description, `X-Robots-Tag`, and measure how much rendered text depends on JavaScript. Only `robots.txt`, meta/header directives, and the raw-fetch results are evaluated; WAF blocking rules are visible only when Fetch itself fails.

## What it checks (grouped by area)

Findings are structured objects (`Finding`) with `area` (access | content | metadata | visibility), `sev` (high | med | low | info), `title`, `evidence`, `fix`, `affects` (ai | search | both), `ties`, `confidence`, and `verify`. Scores are transparent penalty heuristics: `high=15`, `med=7`, `low=3`, `info=0`. Category scores are `100 - sum(penalties)`.

- **Access**: robots.txt bot blocking for search/user/training agents; `noindex`/`nosnippet`/`max-snippet`; missing canonical; redirect chains; sitemap presence/membership; `llms.txt` (reported at low severity intentionally); HTTPS downgrade; bot-challenge pages; unreadable pages.
- **Content**: word count threshold (150 words); headings lost between raw HTML and Fetch markdown; heading-level skips; missing H1 or multiple H1s; image alt coverage; JSON-LD presence and validity (`Article`, `WebPage`, etc.); article date/author exposure; passage coverage for the query terms; CJK bigram fallback.
- **Metadata**: title length (30-60 chars); meta description presence and length; viewport; language declaration; Open Graph tags; Twitter card; canonical mismatch.
- **Visibility**: observed rank for target/topic/brand queries; title rewrite by search; snippet source (meta description vs body); query-term presence in title/H1/first 120 words; competitor comparisons (median word count, headings, tables, lists, dates); topic gaps (topics covered by >=2 competitors but missing from the audited page); same-host overlap; name overlap across domains.

A finding looks like this in the markdown report:

```
### [HIGH] Query terms missing from your H1
_content · affects search_
- Evidence: Missing: ai, call, quality. Top results cover 78% of the terms there; you cover 0%.
- Fix: Work ai, call, quality into the H1 where it reads naturally.
- Typical effect: pages that include the query terms in the title, H1 and opening text are more likely to be matched for that query.
- Confidence: likely | Verify: curl -sL "..." | grep -i "..."
```

## Sample output

Batch mode writes `samples/index.md` with links to each audit. A regression audit for `https://ops-copilot-zeta.vercel.app` is at `samples/regression/ops-copilot.md`. It shows before/after observations: the page has 52 words, no H1 query-term match, no sitemap, no JSON-LD, and ranks outside the top results for the target query; the comparison table shows competitor pages with thousands of words, multiple H2 sections, tables, and lists.

## Honest limits

Scores are disclosed penalty heuristics for prioritising, not benchmarks. Competitor comparisons are correlational, not proof of ranking factors. Rank comes from TinyFish Search and can differ from Google/Bing/AI assistants and varies between runs. The AI-bot list (`BOTS`) will drift; verify against vendor docs before acting on a "blocked" finding. Only `robots.txt` and meta/header directives are evaluated; WAF blocking is visible only when Fetch itself fails. `llms.txt` is reported at low severity on purpose: no major engine documents it as a ranking signal. Offline tests use synthetic data; demos use live pages.

## What this tool does not claim

No indexing or deindexing claims. No ranking causality. No claim about what any specific AI product can or cannot read beyond what `robots.txt` and our own fetches show. Scores are for prioritising only. The audit does not check backlinks, Search Console data, Core Web Vitals, server logs beyond Fetch failures, full index coverage across other search engines, or ranking history.

## What this audit did not check

Backlinks / referring domains; Search Console (Google / Bing) data; Core Web Vitals (LCP, INP, CLS); server logs or user-agent filtering beyond Fetch failures; WAF / bot-blocking rules beyond what Fetch reported; full index coverage or ranking history across other search engines.

## Development

Run tests with synthetic data:

```bash
python -m unittest
```

Tests use a fake TinyFish client (`FakeTF`) and synthetic responses; they verify logic, not the live API. Every bug fix should include a regression test. Reports never overwrite existing files; timestamp suffixes are added automatically. All report strings are redacted: the configured `TINYFISH_API_KEY` and patterns matching `sk-...` or `X-API-Key` values are replaced with `[REDACTED_KEY]`.

---

This tool was built with help from an AI assistant. I reviewed the code, understands how it works, and takes responsibility for its content.
