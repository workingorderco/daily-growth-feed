import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

CONTENT_FILE = Path("content.json")
SUMMARY_FILE = Path("summary.json")
ARCHIVE_DIR = Path("archive")
MAX_HEADLINES = 30

# Leave empty to use the CLI's default model.
# To pin a model use the full API name, e.g. "claude-opus-4-5" or "claude-3-5-sonnet-20241022".
CLAUDE_MODEL = ""


def load_headlines() -> list[str]:
    if not CONTENT_FILE.exists():
        print(f"ERROR: Missing {CONTENT_FILE}. Are you in the repo folder?", file=sys.stderr)
        sys.exit(1)

    data = json.loads(CONTENT_FILE.read_text(encoding="utf-8"))

    headlines: list[str] = []
    for section in data.get("sections", []):
        sec_name = section.get("name", "General")
        for item in section.get("items", []):
            title = (item.get("title") or "").strip()
            if not title:
                continue
            # Prefix with category so the summary has structure
            headlines.append(f"[{sec_name}] {title}")

    # Cap to MAX_HEADLINES
    return headlines[:MAX_HEADLINES]


def build_prompt(headlines: list[str]) -> str:
    # Keep it strict + short to reduce token burn and reduce "chatty" output.
    # Also: explicitly forbid markdown fences, but we still strip them if they appear.
    joined = "\n".join(f"- {h}" for h in headlines)

    return f"""You are producing a compact "one-glance insight" add-on for a personal growth news feed.

INPUT HEADLINES (most recent batch):
{joined}

TASK:
Return ONLY a valid JSON object (no markdown, no backticks, no commentary) with EXACTLY these keys:
- global_theme (string, <= 140 chars)
- market_mood (string, <= 140 chars)  # ok if not finance-heavy; use "risk-on / risk-off" style
- risk_signal (string, <= 180 chars)  # what could plausibly go wrong / what to watch
- opportunity_signal (string, <= 180 chars)  # what could be an upside / lever / advantage
- tight_summaries (array of exactly 10 strings)

tight_summaries rules:
- Exactly 10 items.
- Each item is ONE sentence.
- Each sentence <= 20 words.
- Each item must be grounded in the provided headlines (no made-up facts).
"""


def find_node_cmd() -> list[str] | None:
    """
    Return [node_exe, cli_js_path] by locating the Claude CLI's Node entry point directly.
    Calling node + cli.js bypasses the .cmd batch wrapper, which silently drops piped
    stdin on Windows when invoked via subprocess (a known cmd /c limitation).
    """
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None

    pkg_dir = Path(appdata) / "npm" / "node_modules" / "@anthropic-ai" / "claude-code"
    if not pkg_dir.exists():
        return None

    # Resolve the real entry point from package.json "bin" field
    cli_js: Path | None = None
    pkg_json = pkg_dir / "package.json"
    if pkg_json.exists():
        try:
            meta = json.loads(pkg_json.read_text(encoding="utf-8"))
            bin_field = meta.get("bin", {})
            entries = list(bin_field.values()) if isinstance(bin_field, dict) else [bin_field]
            for rel in entries:
                candidate = (pkg_dir / rel).resolve()
                if candidate.exists():
                    cli_js = candidate
                    break
        except Exception:
            pass

    # Fallback: probe common entry-point names
    if cli_js is None:
        for name in ("cli.js", "index.js", "dist/cli.js", "bin/claude.js"):
            candidate = pkg_dir / name
            if candidate.exists():
                cli_js = candidate
                break

    if cli_js is None:
        return None

    node = shutil.which("node")
    if not node:
        return None

    return [node, str(cli_js)]


def find_claude_exe() -> str | None:
    # 1. Try PATH first (works in any shell where `claude` is on PATH)
    found = shutil.which("claude")
    if found:
        return found

    # 2. Try %APPDATA%\npm\claude.cmd then claude.exe (default npm global install on Windows)
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        for name in ("claude.cmd", "claude.CMD", "claude.exe"):
            candidate = Path(appdata) / "npm" / name
            if candidate.exists():
                return str(candidate)

    return None


def _run_subprocess(cmd: list[str], *, stdin_text: str | None = None) -> str:
    """
    Run cmd as a list (shell=False).
    - For the node-direct path the prompt is already appended to cmd; stdin_text is None.
    - For the shell-mode fallback stdin_text carries the prompt.
    Prints a full diagnostic (exit code, stdout, stderr) and exits on failure.
    """
    try:
        result = subprocess.run(
            cmd,
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
    except FileNotFoundError as e:
        print(f"ERROR: executable not found — {e}", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("ERROR: Claude CLI timed out after 120s.", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        print(f"Claude CLI failed (exit {result.returncode}).", file=sys.stderr)
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        if out:
            print(f"  stdout: {out[:600]!r}", file=sys.stderr)
        if err:
            print(f"  stderr: {err[:600]!r}", file=sys.stderr)
        if not out and not err:
            print("  (no output on stdout or stderr)", file=sys.stderr)
        sys.exit(1)

    return (result.stdout or "").strip()


def call_claude(prompt: str) -> str:
    # --- Primary: node + cli.js with prompt as positional arg (no stdin pipe needed) ---
    node_cmd = find_node_cmd()
    if node_cmd:
        cmd = node_cmd + ["--print"]
        if CLAUDE_MODEL:
            cmd += ["--model", CLAUDE_MODEL]
        cmd.append(prompt)              # prompt as last positional arg, not stdin
        print(f"Using Claude CLI (node): {node_cmd[1]}")
        return _run_subprocess(cmd)

    # --- Fallback: shell=True with .cmd path; prompt via stdin ---
    claude_exe = find_claude_exe()
    if not claude_exe:
        print(
            "ERROR: Claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Using Claude CLI (shell): {claude_exe}")
    # shell=True lets cmd.exe run the .cmd file; prompt goes via stdin to avoid
    # shell-string quoting issues with a multiline prompt.
    try:
        result = subprocess.run(
            f'"{claude_exe}" --print' + (f" --model {CLAUDE_MODEL}" if CLAUDE_MODEL else ""),
            shell=True,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        print("ERROR: Claude CLI timed out after 120s.", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        print(f"Claude CLI failed (exit {result.returncode}).", file=sys.stderr)
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        if out:
            print(f"  stdout: {out[:600]!r}", file=sys.stderr)
        if err:
            print(f"  stderr: {err[:600]!r}", file=sys.stderr)
        if not out and not err:
            print("  (no output on stdout or stderr)", file=sys.stderr)
        sys.exit(1)

    return (result.stdout or "").strip()


def extract_json(raw: str) -> str:
    text = (raw or "").strip()

    # Strip markdown code fences if Claude wraps output in ``` or ```json
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # If it already looks like clean JSON, return as-is
    if text.startswith("{"):
        return text

    # Fallback: scan for first '{' and last '}' to handle any preamble/postamble text
    start = text.find("{")
    end = text.rfind("}")
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
        print("tight_summaries must contain exactly 10 items", file=sys.stderr)
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
    print("Loading headlines...")
    headlines = load_headlines()
    print(f"Extracted {len(headlines)} headlines")

    prompt = build_prompt(headlines)
    print(f"Calling Claude CLI... (prompt chars: {len(prompt)})")

    raw = call_claude(prompt)
    cleaned = extract_json(raw)

    print("Validating JSON...")
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as e:
        print("Invalid JSON returned:", e, file=sys.stderr)
        print("Raw output was:\n", raw, file=sys.stderr)
        sys.exit(1)

    validate_output(result)

    SUMMARY_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    archive_output(result)

    print("Summary saved to summary.json (and archived).")


if __name__ == "__main__":
    main()
