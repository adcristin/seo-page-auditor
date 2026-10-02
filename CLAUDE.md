# SEO Page Auditor (TinyFish bounty build)

## What this is
CLI + library that audits a URL for AI-readability and search visibility using TinyFish Search and Fetch (live pages, never saved copies). Main file: ai_seo_auditor.py. Tests: test_auditor.py and test_regressions.py (offline, fake client, synthetic data).

## Hard rules
- Only dependency: `requests`. Do not add packages without asking.
- Never write, print, log, or commit API keys. Keys come from `.env` or the environment. `.env`, reports, `__pycache__` stay in .gitignore.
- No personal data of other people in code, samples, or tests.
- Windows-safe: never hard-code `/tmp`; use `tempfile`. Always `encoding="utf-8"` when opening text files. No bash-only assumptions in Python.
- Tests are offline. Never call the live TinyFish API from tests.
- Every bug fix and every new finding gets a regression test.
- Findings are dicts: area, sev (high/med/low), title, evidence, fix, affects, ties. Keep that shape.
- Evidence strings must be computed from real data, never templated guesses. No `repr()` in user-facing text.
- Scores are disclosed penalty heuristics, not benchmarks. Do not present them as more.
- Keep changes small and focused. Do not refactor unrelated code. Do not rename public functions or JSON keys without asking.
- After every change run `python -m unittest` and report the result.

## Style
- Plain, readable Python. Short comments explaining WHY, since the author must be able to explain every part.