#!/usr/bin/env python3
"""Spike prototype — TinyFish Agent check, off by default.

Requires: TINYFISH_API_KEY in env.
Usage (manual only — do NOT run automatically):
    python spike_agent_check.py --url URL --check visible-vs-extracted --max-steps 3 --dry-estimate

Design: agent ONLY OBSERVES. Our code verifies every claim against Fetch/HTML.
"""
from __future__ import annotations
import argparse, json, os, sys

AGENT_URL = "https://agent.tinyfish.ai/v1/automation/run"
COST_PER_STEP = 0.015  # USD, from docs


def estimate_cost(steps: int) -> float:
    return steps * COST_PER_STEP


def build_agent_payload(url: str, goal: str, max_steps: int):
    return {
        "url": url,
        "goal": goal,
        "output_schema": {
            "type": "object",
            "properties": {
                "visible_h1": {"type": "string"},
                "visible_first_paragraph": {"type": "string"},
                "overlay_covers_content": {"type": "boolean"},
                "overlay_type": {"type": "string"},
            },
            "required": ["visible_h1", "visible_first_paragraph", "overlay_covers_content", "overlay_type"],
        },
        "agent_config": {"mode": "default"},
        "browser_profile": "lite",
    }


def main():
    parser = argparse.ArgumentParser(description="Agent spike — off by default")
    parser.add_argument("--url", required=True)
    parser.add_argument("--check", choices=["visible-vs-extracted", "hidden-until-click"], default="visible-vs-extracted")
    parser.add_argument("--max-steps", type=int, default=3)
    parser.add_argument("--dry-estimate", action="store_true")
    args = parser.parse_args()

    # Hard cap
    max_steps = min(args.max_steps, 5)
    cost = estimate_cost(max_steps)

    print(f"[SPIKE] URL: {args.url}")
    print(f"[SPIKE] Check: {args.check}")
    print(f"[SPIKE] Hard step cap: {max_steps}")
    print(f"[SPIKE] Estimated cost: ${cost:.3f} (at ${COST_PER_STEP}/step)")
    print(f"[SPIKE] Agent endpoint: {AGENT_URL}")

    if args.dry_estimate:
        print("[SPIKE] --dry-estimate set; no agent call made.")
        return

    # Real agent call
    import requests
    api_key = os.environ.get("TINYFISH_API_KEY", "")
    if not api_key:
        print("[SPIKE] ERROR: TINYFISH_API_KEY not set; aborting agent call.")
        sys.exit(1)
    payload = build_agent_payload(args.url, f"Observe visible H1, first paragraph, and any content-covering overlay. Return structured JSON.", max_steps)
    print(f"[SPIKE] POST {AGENT_URL} ...")
    try:
        r = requests.post(AGENT_URL, headers={"X-API-Key": api_key, "Content-Type": "application/json"}, json=payload, timeout=120)
        print(f"[SPIKE] Status: {r.status_code}")
        print(f"[SPIKE] Response: {r.text[:2000]}")
    except Exception as e:
        print(f"[SPIKE] Agent call failed: {e}")
    sys.exit(0)


if __name__ == "__main__":
    main()
