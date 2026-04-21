#!/usr/bin/env python3
"""
Strip Polymarket + Limitless from rudebot-predictions, leaving Kalshi
(+ Coinbase crypto feed) as the only venues.  Also tightens safety
defaults in config.yaml.example and adds a CI workflow.

Safe:
  * Idempotent — running twice only acts on whatever's still left.
  * Dry-run by default; pass --apply to actually mutate files.
  * Prints every change before it happens.
  * Does NOT commit or push — you review diff, then git yourself.

Usage (from repo root of rudebot-predictions):
    python strip_polymarket.py                # dry-run, lists changes
    python strip_polymarket.py --apply        # make changes
    python -m pytest tests/ -v                # verify nothing broke
    git status
    git add -A
    git commit -m "Strip Polymarket + Limitless; Kalshi-only; tighten safety defaults"
    git push
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Files to delete outright
# ---------------------------------------------------------------------------

DELETE = [
    "connectors/polymarket.py",
    "connectors/limitless.py",
    "engines/polygon_whale.py",
    "tests/test_polygon_whale.py",
    "tests/test_limitless.py",
]


# ---------------------------------------------------------------------------
# Python source scrubbing:
#   * delete any line that imports polymarket / limitless / polygon_whale
#   * delete any line that mentions those classes in a routing branch
#   * warn loudly on any *other* reference so the user can decide manually
# ---------------------------------------------------------------------------

IMPORT_LINE_PATTERNS = [
    re.compile(r"^\s*from\s+[\w.]*connectors\.polymarket\b.*$"),
    re.compile(r"^\s*from\s+[\w.]*connectors\.limitless\b.*$"),
    re.compile(r"^\s*from\s+[\w.]*engines\.polygon_whale\b.*$"),
    re.compile(r"^\s*import\s+[\w.]*connectors\.polymarket\b.*$"),
    re.compile(r"^\s*import\s+[\w.]*connectors\.limitless\b.*$"),
    re.compile(r"^\s*import\s+[\w.]*engines\.polygon_whale\b.*$"),
]

# Lines that are a single statement mentioning these names — safe to drop
# inside registry-style if/elif blocks.  Everything else gets warned about.
SAFE_SIMPLE_LINE_PATTERNS = [
    re.compile(r'^\s*(elif|if)\s+.*(Polymarket|Limitless|polygon_whale|polymarket|limitless).*:\s*$'),
    re.compile(r'^\s*PolymarketConnector\(.*\)\s*$'),
    re.compile(r'^\s*LimitlessConnector\(.*\)\s*$'),
    re.compile(r'^\s*self\.(polymarket|limitless)\s*=.*$'),
    re.compile(r'^\s*[\'"]polymarket[\'"]\s*:\s*\w+.*,?\s*$'),
    re.compile(r'^\s*[\'"]limitless[\'"]\s*:\s*\w+.*,?\s*$'),
]


def scrub_python(path: Path, apply: bool) -> tuple[int, list[str]]:
    """Remove polymarket / limitless import + simple wiring lines.

    Returns (number_of_lines_removed, list_of_warnings_for_caller).
    """
    if not path.exists():
        return 0, []
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    new_lines: list[str] = []
    removed = 0
    warnings: list[str] = []

    for lineno, line in enumerate(lines, 1):
        bare = line.rstrip("\n").rstrip("\r")

        # Drop imports
        if any(p.match(bare) for p in IMPORT_LINE_PATTERNS):
            removed += 1
            print(f"  [drop import] {path}:{lineno}  {bare.strip()}")
            continue

        # Drop safe wiring lines
        if any(p.match(bare) for p in SAFE_SIMPLE_LINE_PATTERNS):
            removed += 1
            print(f"  [drop wiring] {path}:{lineno}  {bare.strip()}")
            continue

        # Warn on any other mention — user decides
        lowered = bare.lower()
        if (
            "polymarket" in lowered
            or "limitless" in lowered
            or "polygon_whale" in lowered
        ):
            # Ignore comments that merely reference Polymarket historically
            # but don't execute anything.
            warnings.append(f"{path}:{lineno}  {bare.strip()}")

        new_lines.append(line)

    if removed and apply:
        path.write_text("".join(new_lines), encoding="utf-8")
    return removed, warnings


# ---------------------------------------------------------------------------
# config.yaml.example — remove polymarket/limitless sections, tighten defaults
# ---------------------------------------------------------------------------

def scrub_config_example(path: Path, apply: bool) -> bool:
    if not path.exists():
        print(f"[warn] {path} not found — skipping config scrub")
        return False
    src = path.read_text(encoding="utf-8")
    original = src

    # Remove top-level "polymarket:" and "limitless:" blocks.
    # A block ends at the next top-level key (line starting at column 0 without leading space)
    # or EOF.  We identify blocks by "^key:" followed by indented lines.
    def drop_section(text: str, key: str) -> str:
        pattern = re.compile(
            rf"(?ms)^{re.escape(key)}:\s*(?:\n(?:[ \t]+.*\n?|\n)*?)(?=^\S|\Z)",
        )
        return pattern.sub("", text)

    for key in ("polymarket", "limitless"):
        src = drop_section(src, key)

    # Tighten safety defaults (in whatever section they live).
    # Only modify keys we care about; leave indent/comments alone.
    def set_key(text: str, key: str, new_value: str) -> str:
        return re.sub(
            rf"(?m)^(\s*){re.escape(key)}:\s*[^\n#]*(\s*(?:#.*)?)$",
            rf"\1{key}: {new_value}\2",
            text,
            count=1,
        )

    src = set_key(src, "mode", "paper")
    src = set_key(src, "require_confirm", "true")
    src = set_key(src, "max_position_usd", "10.00")
    # daily_loss_limit_usd default per SAFETY.md
    src = set_key(src, "daily_loss_limit_usd", "20.00")

    if src == original:
        print(f"[skip] {path}: no changes needed")
        return False

    print(f"[apply] {path}: polymarket/limitless sections stripped; safety defaults tightened")
    if apply:
        path.write_text(src, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Add CI workflow (validate.yml)
# ---------------------------------------------------------------------------

VALIDATE_YML = '''name: Validate

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  workflow_dispatch:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install pytest pyyaml
          if [ -f requirements.txt ]; then pip install -r requirements.txt; fi

      - name: Run tests
        run: |
          PYTHONPATH=. pytest tests/ -v --tb=short

      - name: Check paper-mode default (safety gate)
        run: |
          python - <<'PY'
          import sys
          import yaml
          from pathlib import Path
          path = Path("config.yaml.example")
          if not path.exists():
              print(f"SAFETY BLOCKER: {path} is missing")
              sys.exit(1)
          cfg = yaml.safe_load(path.read_text()) or {}
          mode = cfg.get("mode")
          if mode != "paper":
              print(f"SAFETY BLOCKER: mode is '{mode}', must be 'paper' in committed config.yaml.example")
              sys.exit(1)
          print(f"mode: paper (ok)")
          safety = cfg.get("safety", {}) or {}
          if cfg.get("require_confirm") is False or safety.get("require_confirm") is False:
              print("WARNING: require_confirm appears to be false in committed example")
          PY
'''


def add_validate_yml(apply: bool) -> bool:
    path = ROOT / ".github" / "workflows" / "validate.yml"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if "Check paper-mode default" in existing:
            print(f"[skip] {path.relative_to(ROOT)}: already present with safety gate")
            return False
    print(f"[apply] write {path.relative_to(ROOT)} (pytest + paper-mode safety gate)")
    if apply:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(VALIDATE_YML, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually modify files (default is dry-run)")
    args = ap.parse_args()

    if not (ROOT / "main.py").exists():
        sys.exit(f"[error] run from rudebot-predictions repo root "
                 f"(expected {ROOT / 'main.py'} to exist)")

    print("=" * 60)
    print(f"strip_polymarket.py  {'APPLYING' if args.apply else 'DRY-RUN'}")
    print("=" * 60)

    # 1. delete polymarket / limitless / polygon_whale files
    print("\n[1/4] delete files")
    for rel in DELETE:
        p = ROOT / rel
        if p.exists():
            print(f"  [delete] {rel}")
            if args.apply:
                p.unlink()
        else:
            print(f"  [skip] {rel} (already gone)")

    # 2. scrub python files for remaining references
    print("\n[2/4] scrub python files for polymarket/limitless references")
    all_warnings: list[str] = []
    # Walk all .py files under ROOT (excluding the script itself and deleted targets)
    skip_rel = {Path(r).as_posix() for r in DELETE}
    for py in sorted(ROOT.rglob("*.py")):
        rel = py.relative_to(ROOT).as_posix()
        if rel == "strip_polymarket.py" or rel in skip_rel:
            continue
        # Skip virtualenv / cache dirs
        if any(part.startswith(".") or part in {"__pycache__", "venv", ".venv"}
               for part in py.parts):
            continue
        removed, warnings = scrub_python(py, args.apply)
        all_warnings.extend(warnings)

    # 3. scrub config.yaml.example (and any config.yml / config.yaml if present)
    print("\n[3/4] scrub config.yaml.example + tighten safety defaults")
    for name in ("config.yaml.example", "config.yaml", "config.yml"):
        scrub_config_example(ROOT / name, args.apply)

    # 4. add CI workflow
    print("\n[4/4] add CI workflow (pytest + safety gate)")
    add_validate_yml(args.apply)

    # Summary + warnings
    print("\n" + "=" * 60)
    if all_warnings:
        print(f"{len(all_warnings)} remaining references (review manually):")
        for w in all_warnings:
            print(f"  {w}")
        print()
        print("These are not auto-removed because they might be legitimate")
        print("(comment/docstring/log message).  Open each and delete by hand")
        print("if it's executing code, leave alone if it's documentation.")
    else:
        print("No remaining polymarket/limitless references in source.")

    if not args.apply:
        print("\nDRY-RUN: no files modified.  Re-run with --apply to commit changes.")
    else:
        print("\nDone.  Next:")
        print("    PYTHONPATH=. pytest tests/ -v     # verify nothing broke")
        print("    git status")
        print("    git diff --stat")
        print("    git add -A")
        print("    git commit -m 'Strip Polymarket + Limitless; Kalshi-only; tighten safety defaults'")
        print("    git push")


if __name__ == "__main__":
    main()

