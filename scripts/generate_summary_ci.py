"""
generate_summary_ci.py
======================
CI-compatible summary generator for GitHub Actions.
Uses the Anthropic Python SDK (pip install anthropic) instead of the
local Claude CLI, so it works in any environment with ANTHROPIC_API_KEY set.

Usage:
    pip install anthropic
    ANTHROPIC_API_KEY=<key> python scripts/generate_summary_ci.py
    (or set ANTHROPIC_API_KEY in GitHub Actions secrets)
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

CONTENT_FILE = Path("content.json")
SUMMARY_FILE = Path("summary.json")
ARCHIVE_DIR  = Path("archive")
MAX_HEADLINES = 30
MODEL = "claude-opus-4-5"   # pin a stable model for CI predictability


def load_headlines() -> list[str]:
    if not CONTENT_FILE.exists():
        print(f"ERROR: {CONTENT_FILE} not found. Run scripts/build_feed.py first.", file=sys.stderr)
        sys.exit(1)
    data = json.loads(CONTENT_FILE.read_text(encoding="utf-8"))
    headlines: list[str] = []
    for section in data.get("sections", []):
        sec_name = section.get("name", "General")
        for item in section.get("items", []):
            title = (item.get("title") or "").strip()
            if title:
                headlines.append(f"[{sec_name}] {title}")
    return headlines[:MAX_HEADLINES]


def build_prompt(headlines: list[str]) -> str:
    joined = "\n".join(f"- {h}" for h in headlines)
    return f"""You are producing a compact "one-glance insight" add-on for a personal growth news feed.

INPUT HEADLINES (most recent batch):
{joined}

TASK:
Return ONLY a valid JSON object (no markdown, no backticks, no commentary) with EXACTLY these keys:
- global_theme (string, <= 140 chars)
- market_mood (string, <= 140 chars)
- risk_signal (string, <= 180 chars)
- opportunity_signal (string, <= 180 chars)
- tight_summaries (array of exactly 10 strings)

tight_summaries rules:
- Exactly 10 items.
- Each item is ONE sentence.
- Each sentence <= 20 words.
- Each item must be grounded in the provided headlines (no made-up facts).
"""


def call_claude(prompt: str) -> str:
    try:
        import anthropic
    except ImportError:
        print("ERROR: anthropic package not installed. Run: pip install anthropic", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def extract_json(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if text.startswith("{"):
        return text
    start = text.find("{")
    end   = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


def validate_output(obj: dict):
    required = {"global_theme", "market_mood", "risk_signal", "opportunity_signal", "tight_summaries"}
    missing = required - set(obj.keys())
    if missing:
        print("Missing fields in response:", missing, file=sys.stderr)
        sys.exit(1)
    if not isinstance(obj["tight_summaries"], list):
        print("tight_summaries must be a list", file=sys.stderr)
        sys.exit(1)
    if len(obj["tight_summaries"]) != 10:
        print(f"tight_summaries must have 10 items, got {len(obj['tight_summaries'])}", file=sys.stderr)
        sys.exit(1)
    for i, line in enumerate(obj["tight_summaries"], start=1):
        if not isinstance(line, str) or not line.strip():
            print(f"Invalid tight_summaries item #{i}", file=sys.stderr)
            sys.exit(1)


def archive_output(data: dict):
    ARCHIVE_DIR.mkdir(exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%SZ")
    archive_path = ARCHIVE_DIR / f"{date_str}_summary.json"
    archive_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def main():
    print("Loading headlines from content.json...")
    headlines = load_headlines()
    print(f"Extracted {len(headlines)} headlines")

    prompt = build_prompt(headlines)
    print(f"Calling Claude API (model={MODEL})...")

    raw     = call_claude(prompt)
    cleaned = extract_json(raw)

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as e:
        print("Invalid JSON returned:", e, file=sys.stderr)
        print("Raw output:\n", raw, file=sys.stderr)
        sys.exit(1)

    validate_output(result)

    SUMMARY_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    archive_output(result)
    print(f"summary.json written ({len(result['tight_summaries'])} key points).")


if __name__ == "__main__":
    main()
