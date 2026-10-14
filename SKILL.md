# SEO Auditor Skill (ai_seo_auditor)

Use when user asks whether a URL is readable by AI search/fetch tools or why it does not rank for a query.

## Required environment

- `TINYFISH_API_KEY` (set in `.env` or env; never commit keys)
- Python 3 + `requests` (`pip install requests`)
- Only dependency: `requests`

## Exact commands (verified from argparse)

```bash
python ai_seo_auditor.py <url> -q "target query" -o report
python ai_seo_auditor.py --batch urls.csv --batch-delay 3 --country IN
python ai_seo_auditor.py <url> --no-raw -o report_no_raw
python ai_seo_auditor.py <url> --html --debug-parity -o report
python -m unittest        # offline tests only; never call live API
```

Flags from `ai_seo_auditor.py`: `-q/--query`, `-o/--out` (default `report`), `--country`, `--language`, `--no-raw`, `--batch FILE`, `--batch-delay`, `--timeout` (default 30), `--retries` (default `RETRY_MAX_ATTEMPTS`), `--html`, `--debug-parity`. Positional query words allowed without `-q`.

## Architecture notes (verified from source)

- Single-file CLI (`ai_seo_auditor.py`) + offline unittest suite. No extra packages without asking.
- `audit(url, query, tf, ...)` is the single entry point; returns dict with `scores`, `findings`, `competitors`, `topic_gaps`, `calls`.
- `TinyFish` wrapper logs calls (`calls` list) for provenance.
- `normalize_target()` validates public URLs (rejects localhost/private IPs, embedded credentials, secret query params, unsupported schemes).
- `raw_view()` does plain HTTP GET (max 3 MB, max 5 redirects, SSRF-protected) for JSON-LD and JS dependence. Skip with `--no-raw`.
- Reports write `<out>.md` + `<out>.json`; never overwrite — timestamp suffix added. Batch writes `samples/<slug>.md` + `.json` and `samples/index.md`.
- Key redaction: `API_KEY_VALUE` and patterns `sk-...`, `X-API-Key` become `[REDACTED_KEY]`. Never print real keys.
- `.env`, `.claude/`, `__pycache__/`, `samples/regression/` in `.gitignore`.
- Exit codes: `0` complete, `1` input/config error, `2` partial/unavailable (404, error, unreadable).

## How to read JSON output

Key fields: `scores` (`ai_readability`, `search_visibility`), `category_scores` (access/content/metadata/visibility), `fix_first` (top 3 critical findings), `findings` (objects with `area`, `sev`, `title`, `evidence`, `fix`, `affects`, `ties`, `confidence`, `verify`), `competitors` (median word count, headings, tables, lists, dates), `topic_gaps` (topics from >=2 competitors missing from audited page), `calls` (TinyFish call log), `rank`, `page` profile.

Findings use penalty heuristics: `high=15`, `med=7`, `low=3`, `info=0`. Category score = `100 - sum(penalties)`.

Every finding = `Finding(area, sev, title, evidence, fix, affects, ties, confidence, verify)`. Evidence computed from real data, never templated guesses. No `repr()` in user-facing text.

## What future agents commonly miss

- Tests are synthetic (`FakeTF`), not integration tests. Do not add live API dependencies. After every fix: `python -m unittest` must pass and add a regression test.
- `llms.txt` finding is low severity by design; do not promote it.
- `fix_first` selects top 3 findings by severity (`high` > `med` > `low` > `info`) then `affects == "both"`; excludes `"No llms.txt"` and `"Meta description length"`.
- If changing public JSON keys or `Finding` fields, check `tests/test_auditor.py` references.

## Summarise for a human in 5 lines

1. Readability/visibility scores (`/100`) and rank for query.
2. Count `findings` by severity; state the top `fix_first` items.
3. Note if page unreadable (404 / fetch error) or non-HTML.
4. Mention competitor gaps (`topic_gaps`, median words/headings) and any bot-blocking.
5. End with: scores are prioritisation heuristics, not ranking guarantees; live results vary between runs.

## Limits (honest)

- Scores are disclosed penalties, not benchmarks.
- Competitor comparisons are correlational, not proof of ranking factors.
- Rank comes from TinyFish Search and differs from Google/Bing/AI assistants; varies between runs.
- `BOTS` list drifts; verify against vendor docs.
- Only `robots.txt` and meta/header directives evaluated; WAF blocking visible only when Fetch fails.
- `llms.txt` reported at low severity intentionally (no major engine documents it as ranking signal).
- Reports never overwrite existing files; timestamp suffix added automatically.
- All report strings redacted: `TINYFISH_API_KEY` and `sk-...` / `X-API-Key` patterns replaced with `[REDACTED_KEY]`.
