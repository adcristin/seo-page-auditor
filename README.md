# AI-readability & search-visibility auditor

Give it a URL (and optionally the query you want the page to win). It reads the page the way an AI tool does, checks how the page shows up in search, and reports what's missing with fixes a site owner can apply the same day.

```bash
pip install requests
export TINYFISH_API_KEY=...            # free at agent.tinyfish.ai/api-keys
python ai_seo_auditor.py https://example.com/blog/post -q "target query" -o report
# -> report.md (human) + report.json (machine); add --country IN for geo-targeted search\n```\n\nTo regenerate the reference regression samples, run:\n`python scripts/rerun_reference_audits.py`

## How TinyFish is used

| Call | Endpoint | Why it matters |
|---|---|---|
| Page, `format=markdown`, `page_metadata`, `links`, `ttl=0` | **Fetch** | The exact text, headings, tables, canonical, robots directives, OG tags and date/author an AI tool gets after a real browser renders the page. Live, never cached. |
| Page, `format=html`, `include_selectors=["img"]` | **Fetch** | Image alt-text coverage. Images with no alt are invisible to AI tools. |
| `/robots.txt`, `/llms.txt`, `sitemap.xml` (+ child sitemaps) | **Fetch** | Which search, user-fetch and training bots are allowed on this path; whether the page is in the sitemap. Fetch parses sitemaps into URL lists. |
| Target query, pages 0-2 | **Search** | The page's rank, and the title and snippet the engine displays for it. |
| `site:<domain> <page title>` (only when the page is not found) | **Search** | Separates "indexed but weak" from "not indexed", which need different fixes. |
| Top 3 competing results | **Fetch** | The pages that outrank you are read with the same call and scored on the same yardstick. This is the readability-to-visibility link: query terms in title/H1/opening text, depth, tables, lists, exposed dates. |

Search and Fetch are free; the auditor makes roughly 8 to 12 calls per page.

One non-TinyFish call: a plain HTTP GET of the page. Fetch renders JavaScript and strips `<script>` tags, so it cannot show JSON-LD or tell you which text depends on JS. The raw GET does both. Use `--no-raw` to skip it.

## What the report contains

1. Verdict: AI-readability score, search rank for the query, finding counts.
2. What an AI tool extracts: words, headings, meta, canonical, JSON-LD, images and alt, links, bots blocked, plus the first 500 characters as extracted.
3. How it shows up: rank, displayed title/snippet versus your real title/description.
4. Comparison table: you versus the pages above you.
5. Findings ordered by severity, each with evidence from this page, a fix (generated JSON-LD and title starting points where useful), and why it costs visibility.
6. Provenance: every TinyFish call made.

## Known limits (read before trusting the numbers)

- Scores are simple, disclosed heuristics (penalty-based), meant for prioritising, not benchmarking.
- Rank is TinyFish Search's rank. It can differ from Google, Bing or an AI assistant's citations, and from your location or personalisation.
- Competitor comparison is correlational: lengths and structure are patterns among winners, not proven ranking factors.
- The bot list in `BOTS` drifts; check vendor docs before acting on a "blocked" finding.
- Only `robots.txt` and meta/header directives are evaluated; WAF rules that block bots are not visible except when Fetch itself fails.
- `llms.txt` is reported as low severity on purpose: no major engine documents it as a ranking signal.

## Exit codes

- `0` — complete observation (audit finished, report written).
- `1` — input/config error (bad URL, missing API key, unsupported scheme, batch file missing, embedded credentials, query secrets, etc.).
- `2` — partial or unavailable (page unreadable, search failed, 404, redirect loop exceeded, raw GET blocked by SSRF check).

These are documented in `--help` and used by `sys.exit`.

## Security / SSRF protections

- `raw_view()` resolves hostnames before connecting and refuses private, loopback, link-local, reserved, multicast, and cloud-metadata IPs (IPv4 and IPv6).
- Redirects are followed manually (max 5 hops); the same IP checks apply to every hop.
- URLs with embedded credentials (`user:pass@`) or query secrets (`token`, `key`, `signature`, `password`) are rejected before any request.
- Reports never overwrite existing files; a UTC timestamp suffix (`_YYYYMMDD_HHMMSS`) is added when a file exists, and the final path is printed.
- Every string written to reports, logs, and exceptions is redacted: the configured `TINYFISH_API_KEY` value and patterns matching `sk-...` or `X-API-Key` header values are replaced with `[REDACTED_KEY]`.

## Batch mode (`--batch FILE`)

FILE is a text or CSV file with one `url[,query]` per line. Lines starting with `#` are comments; blank lines are ignored. If the query is omitted, it is derived from the page.

```bash
python ai_seo_auditor.py --batch urls.csv --batch-delay 3
```

Batch writes `samples/<slug>.md` and `.json` (slug derived from hostname + path, filesystem-safe), then `samples/index.md` with a table of results, counts, and links. It respects `--batch-delay` (default 3 s) and continues after per-URL errors. The API key is never written to any batch output.

## Tests

`python -m unittest` runs offline checks with a fake client. The data is synthetic, shaped like real Fetch/Search responses; it verifies the logic, not the live API.
