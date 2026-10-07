# Implementation Plan: Robust Error Handling and Retries for AI SEO Auditor

## Overview
Improve the stability of `ai_seo_auditor.py` by adding exponential backoff to the `TinyFish` client, graceful degradation in the `audit()` function, and improved exception handling in `main()`.

## 1. Constants to Add
At the top of `ai_seo_auditor.py`:
- `RETRY_MAX_TRIES = 3`
- `RETRY_BASE_DELAY = 2.0`
- `RETRY_JITTER_MAX = 1.0`
- `DEFAULT_TIMEOUT = 30`
- `DEFAULT_RETRIES = 3`

## 2. Exponential Backoff in `TinyFish`
### Strategy: Helper Method
Instead of a decorator (which can be tricky with `self` and `requests.Session`), I will implement a `_request_with_retry` helper method within the `TinyFish` class.

### Logic:
- Wrap `self.s.get` and `self.s.post` calls.
- Loop up to `self.retries` times.
- Catch `requests.Timeout` and `requests.ConnectionError`.
- Check `r.status_code` for `429` and `5xx`.
- If retryable:
    - Check `Retry-After` header for an explicit delay.
    - Otherwise, use `RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, RETRY_JITTER_MAX)`.
- Raise `r.raise_for_status()` on the final attempt.

## 3. Graceful Degradation in `audit()`
### a. Competitor Fetch Failures
- Wrap the competitor `tf.fetch` and subsequent processing in a try-except block.
- Ensure any failure is logged to `comp_err` and the audit continues.

### b. Search Failures
- Wrap `tf.search` calls in try-except.
- If search fails:
    - Set `rank = None`.
    - Add a low-severity finding: "Search unavailable, visibility not measured".

### c. Main Page Fetch Failure
- If the initial `tf.fetch` for the target URL fails completely after retries, `audit()` should return a result with status "error" (or similar) and a high-severity finding.
- `main()` will then use this to print a friendly message.

## 4. Improved `main()` Exception Handling
- Replace `except requests.HTTPError` with `except requests.RequestException`.
- Catch any custom `TinyFish` errors if defined.
- Print a clean, one-line error to `sys.stderr` (avoiding API keys/headers).
- Exit with code 2.

## 5. Configurability (CLI Flags)
- Add `--timeout` and `--retries` to `argparse`.
- Pass these values to the `TinyFish` constructor: `TinyFish(key, timeout=a.timeout, retries=a.retries)`.
- Update `TinyFish.__init__` to store these settings.

## 6. Testing Strategy in `test_auditor.py`
Create a `FaultyTF` class inheriting from `FakeTF` to simulate:
- **Transient Failure**: Throw `requests.Timeout` on 1st call, success on 2nd.
- **Persistent 429**: Always return 429.
- **Connection Error**: Throw `requests.ConnectionError`.
- **Mixed Failure**: Success for main page, but failure for competitor fetches.

### New Test Cases:
- `test_retry_logic_success_after_timeout`
- `test_retry_logic_exhaustion_on_429`
- `test_audit_continues_on_competitor_failure`
- `test_audit_handles_search_outage`

## Critical Files for Implementation
- `ai_seo_auditor.py`
- `test_auditor.py`
