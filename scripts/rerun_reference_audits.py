#!/usr/bin/env python3
import os
import time
import json
import sys
from pathlib import Path
from collections import Counter

# Import the auditor library functions
from ai_seo_auditor import audit, to_markdown, TinyFish, load_dotenv

# Stay well under the free Search limit (~30 queries/min)
AUDIT_DELAY_SECONDS = 5

# Reference pairs: (url, query, slug)
REFERENCE_SITES = [
    ("https://www.tinyfish.ai", "web fetch api for ai agents", "tinyfish"),
    ("https://en.wikipedia.org/wiki/Search_engine_optimization", "what is seo", "wiki-seo"),
    ("https://docs.python.org/3/library/urllib.request.html", "python requests library", "python-urllib"),
    ("https://ops-copilot-zeta.vercel.app", "ai call quality", "ops-copilot"),
]

def main():
    load_dotenv()
    key = os.environ.get("TINYFISH_API_KEY")
    if not key:
        sys.exit("Set TINYFISH_API_KEY (free at agent.tinyfish.ai/api-keys).")

    tf = TinyFish(key)
    output_dir = Path("samples/regression")
    output_dir.mkdir(parents=True, exist_ok=True)

    results_summary = []

    print(f"Starting reference audits ({len(REFERENCE_SITES)} sites)...")

    for url, query, slug in REFERENCE_SITES:
        print(f"Auditing {url}...", end=" ", flush=True)
        try:
            # Use the library audit function directly
            res = audit(url, query, tf)

            # Write markdown report
            md_path = output_dir / f"{slug}.md"
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(to_markdown(res))

            # Write JSON data
            json_path = output_dir / f"{slug}.json"
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(res, f, indent=2, default=str)

            # Collect data for final table
            findings_sev = Counter(f["sev"] for f in res.get("findings", []))
            results_summary.append({
                "url": url,
                "ai": res["scores"]["ai_readability"],
                "vis": res["scores"]["search_visibility"],
                "high": findings_sev["high"],
                "med": findings_sev["med"],
                "low": findings_sev["low"]
            })
            print("Done.")

        except Exception as e:
            print(f"FAILED: {e}")

        # Sleep to avoid rate limits
        time.sleep(AUDIT_DELAY_SECONDS)

    # Print final summary table
    print("\nSummary Table:")
    print(f"| {'URL':<45} | {'AI':<5} | {'Vis':<5} | {'H':<3} | {'M':<3} | {'L':<3} |")
    print("-" * 75)
    for s in results_summary:
        print(f"| {s['url']:<45} | {s['ai']:<5} | {s['vis']:<5} | {s['high']:<3} | {s['med']:<3} | {s['low']:<3} |")

if __name__ == "__main__":
    main()
