#!/usr/bin/env python3
"""
apply_polymarket_strip_v2.py — aggressive, Kalshi-only cleanup.

Upgrade over v1: removes multi-line `if "polymarket" in ...:` blocks, cross-
reference blocks that require both Kalshi AND Polymarket, dict entries like
`"polymarket": ...,` inside platform registries, and research.polymarket_gamma.

Idempotent + dry-run-default + prints every change before touching disk.

Usage (from rudebot-predictions repo root):
    python apply_polymarket_strip_v2.py                 # dry run
    python apply_polymarket_strip_v2.py --apply         # actually modify
    PYTHONPATH=. python -m pytest tests/ -v             # verify
    git add -A && git commit -m "Kalshi-only strip v2" && git push
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

DELETE = [
    "connectors/polymarket.py",
    "connectors/limitless.py",
    "engines/polygon_whale.py",
    "tests/test_polygon_whale.py",
    "tests/test_limitless.py",
    "research/polymarket_gamma.py",
]

# --------------------------------------------------------------------------
# Line-level removals (import + one-line assignments + single-entry dict keys)
# --------------------------------------------------------------------------

LINE_DROP = [
    re.compile(r'^\s*from\s+[\w.]*connectors\.polymarket\b.*$'),
    re.compile(r'^\s*from\s+[\w.]*connectors\.limitless\b.*$'),
    re.compile(r'^\s*from\s+[\w.]*engines\.polygon_whale\b.*$'),
    re.compile(r'^\s*from\s+[\w.]*polymarket_gamma\b.*$'),
    re.compile(r'^\s*import\s+[\w.]*connectors\.polymarket\b.*$'),
    re.compile(r'^\s*import\s+[\w.]*connectors\.limitless\b.*$'),
    re.compile(r'^\s*import\s+[\w.]*engines\.polygon_whale\b.*$'),
    re.compile(r'^\s*self\.polymarket\s*=.*$'),
    re.compile(r'^\s*self\.limitless\s*=.*$'),
    re.compile(r'^\s*PolymarketConnector\(.*\)\s*$'),
    re.compile(r'^\s*LimitlessConnector\(.*\)\s*$'),
    re.compile(r'^\s*[\'"]polymarket[\'"]\s*:\s*[\w\[\]\'"\(\)\., ]+,?\s*$'),
    re.compile(r'^\s*[\'"]limitless[\'"]\s*:\s*[\w\[\]\'"\(\)\., ]+,?\s*$'),
    re.compile(r'^\s*[\'"]polymarket[\'"]\s*,\s*$'),
    re.compile(r'^\s*[\'"]limitless[\'"]\s*,\s*$'),
]


def drop_if_block(text: str, predicate: str) -> tuple[str, int]:
    """Drop any `if <predicate>:` block (and its body) from text.

    Uses indent-based block detection.  Returns (new_text, count_removed).
    Handles `if`, `elif`, and standalone blocks.  Leaves surrounding code alone.
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    removed = 0
    i = 0
    pat = re.compile(rf'^(\s*)(?:if|elif)\s+.*{predicate}.*:\s*(?:#.*)?$')
    while i < len(lines):
        m = pat.match(lines[i].rstrip("\n"))
        if m:
            indent = len(m.group(1))
            start = i
            i += 1
            while i < len(lines):
                line = lines[i]
                stripped = line.strip()
                if not stripped:
                    i += 1
                    continue
                # Re-examine indent; if <= header indent, we exited the block
                cur_indent = len(line) - len(line.lstrip())
                if cur_indent <= indent:
                    break
                i += 1
            removed += (i - start)
            continue
        out.append(lines[i])
        i += 1
    return "".join(out), removed


def drop_else_comment_block(text: str) -> tuple[str, int]:
    """Drop trailing `else:\\n    # polymarket ... skip for now\\n    continue` style block."""
    pattern = re.compile(
        r'(?m)^(\s*)else:\s*\n'
        r'\s+#[^\n]*(?:polymarket|limitless)[^\n]*\n'
        r'\s+continue\s*\n',
        re.IGNORECASE,
    )
    new_text, n = pattern.subn("", text)
    return new_text, n


def scrub_python(path: Path, apply: bool) -> tuple[int, list[str]]:
    if not path.exists():
        return 0, []
    src = path.read_text(encoding="utf-8")
    original = src

    # 1. block-level: drop `if polymarket in ...:` and `elif polymarket ...:`
    for pred in (r'"polymarket"', r"'polymarket'", r'"limitless"', r"'limitless'"):
        src, _ = drop_if_block(src, pred)

    # 1b. Cross-reference if-block requiring both kalshi AND polymarket
    src = re.sub(
        r'(?m)^(\s*)if\s+"kalshi"\s+in\s+[^\n]*\s+and\s+"polymarket"\s+in\s+[^\n]*:\s*\n'
        r'(?:\1\s+[^\n]*\n|\s*\n)+?'
        r'(?=^\1\S|^\s*\n\S|\Z)',
        "",
        src,
    )

    # 2. trailing else-block that mentions polymarket
    src, _ = drop_else_comment_block(src)

    # 3. line-level: drop imports, assignments, dict keys
    lines = src.splitlines(keepends=True)
    kept: list[str] = []
    dropped_lines = 0
    for line in lines:
        bare = line.rstrip("\n").rstrip("\r")
        if any(p.match(bare) for p in LINE_DROP):
            dropped_lines += 1
            continue
        kept.append(line)
    src = "".join(kept)

    if src == original:
        return 0, []

    # Count remaining polymarket/limitless/polygon_whale references as warnings
    warnings: list[str] = []
    for i, line in enumerate(src.splitlines(), 1):
        low = line.lower()
        if "polymarket" in low or "limitless" in low or "polygon_whale" in low:
            warnings.append(f"{path}:{i}  {line.strip()[:140]}")

    total_removed = len(original.splitlines()) - len(src.splitlines())
    print(f"[apply] {path}: -{total_removed} lines")
    if apply:
        path.write_text(src, encoding="utf-8")
    return total_removed, warnings


# --------------------------------------------------------------------------
# config.yaml.example — same logic as v1
# --------------------------------------------------------------------------

def scrub_config_example(path: Path, apply: bool) -> bool:
    if not path.exists():
        return False
    src = path.read_text(encoding="utf-8")
    original = src

    for key in ("polymarket", "limitless"):
        src = re.sub(
            rf"(?ms)^{re.escape(key)}:\s*(?:\n(?:[ \t]+.*\n?|\n)*?)(?=^\S|\Z)",
            "",
            src,
        )

    def set_key(text: str, key: str, value: str) -> str:
        return re.sub(
            rf"(?m)^(\s*){re.escape(key)}:\s*[^\n#]*(\s*(?:#.*)?)$",
            rf"\1{key}: {value}\2",
            text,
            count=1,
        )

    src = set_key(src, "mode", "paper")
    src = set_key(src, "require_confirm", "true")
    src = set_key(src, "max_position_usd", "10.00")
    src = set_key(src, "daily_loss_limit_usd", "20.00")

    if src == original:
        print(f"[skip] {path}: no changes")
        return False
    print(f"[apply] {path}: polymarket+limitless sections stripped; safety defaults tightened")
    if apply:
        path.write_text(src, encoding="utf-8")
    return True


# --------------------------------------------------------------------------
# validate.yml
# --------------------------------------------------------------------------

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
      - name: Paper-mode safety gate
        run: |
          python - <<'PY'
          import sys, yaml
          from pathlib import Path
          path = Path("config.yaml.example")
          if not path.exists():
              print("SAFETY: config.yaml.example missing"); sys.exit(1)
          cfg = yaml.safe_load(path.read_text()) or {}
          if cfg.get("mode") != "paper":
              print(f"SAFETY BLOCKER: mode={cfg.get('mode')!r}, must be 'paper'"); sys.exit(1)
          print("mode: paper (ok)")
          PY
'''


def add_validate_yml(apply: bool) -> bool:
    path = ROOT / ".github" / "workflows" / "validate.yml"
    if path.exists() and "Paper-mode safety gate" in path.read_text(encoding="utf-8"):
        print(f"[skip] {path.relative_to(ROOT)}: already present")
        return False
    print(f"[apply] write {path.relative_to(ROOT)}")
    if apply:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(VALIDATE_YML, encoding="utf-8")
    return True


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if not (ROOT / "main.py").exists():
        sys.exit(f"[error] run from rudebot-predictions repo root")

    print("=" * 60)
    print(f"apply_polymarket_strip_v2.py  {'APPLYING' if args.apply else 'DRY-RUN'}")
    print("=" * 60)

    # 1. delete files
    print("\n[1/4] delete polymarket/limitless/polygon_whale files")
    for rel in DELETE:
        p = ROOT / rel
        if p.exists():
            print(f"  [delete] {rel}")
            if args.apply:
                p.unlink()
        else:
            print(f"  [skip] {rel}")

    # 2. scrub .py files
    print("\n[2/4] scrub .py files (imports + blocks + dict keys)")
    all_warnings: list[str] = []
    skip_rel = {Path(r).as_posix() for r in DELETE}
    for py in sorted(ROOT.rglob("*.py")):
        rel = py.relative_to(ROOT).as_posix()
        if rel in skip_rel or rel.startswith((
            "apply_polymarket_strip",
            "venv/", ".venv/",
        )):
            continue
        if any(part.startswith(".") or part in {"__pycache__", "venv", ".venv"}
               for part in py.parts):
            continue
        _, warnings = scrub_python(py, args.apply)
        all_warnings.extend(warnings)

    # 3. config
    print("\n[3/4] scrub config.yaml.example")
    for name in ("config.yaml.example", "config.yaml", "config.yml"):
        scrub_config_example(ROOT / name, args.apply)

    # 4. CI
    print("\n[4/4] add validate.yml")
    add_validate_yml(args.apply)

    print("\n" + "=" * 60)
    if all_warnings:
        print(f"{len(all_warnings)} residual references remain (review manually):")
        for w in all_warnings[:30]:
            print(f"  {w}")
        if len(all_warnings) > 30:
            print(f"  ...and {len(all_warnings) - 30} more")
    else:
        print("Zero residual polymarket/limitless references.  Ready to commit.")

    if not args.apply:
        print("\nDRY-RUN: no files modified.  Re-run with --apply.")
    else:
        print("\nNext: PYTHONPATH=. pytest tests/ -v && git add -A && git commit && git push")


if __name__ == "__main__":
    main()

