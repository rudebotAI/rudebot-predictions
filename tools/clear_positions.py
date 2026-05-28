#!/usr/bin/env python3
"""
tools/clear_positions.py — Flush stale paper positions from the state store.

Why this exists: the bot logs show it stuck at "Max positions: 5/5" while
still finding +EV opportunities. If those positions are real Kalshi trades,
DO NOT run this — close them through Kalshi first. If they are stale paper
positions left from a previous run, this is the fastest way to unblock.

Usage on Railway:
    railway run python tools/clear_positions.py --dry-run    # show what would be cleared
    railway run python tools/clear_positions.py --confirm    # actually clear

Locally:
    BOT_MODE=paper python tools/clear_positions.py --dry-run
"""

import argparse
import json
import os
import sys
from pathlib import Path

# The state store path used by execution.state_store. If your deployment
# mounts a Railway volume, ensure this path matches that mount.
STATE_FILE_CANDIDATES = [
    Path(os.environ.get("STATE_FILE", "")),
    Path("/app/state/state.json"),
    Path("state.json"),
    Path("logs/state.json"),
]


def find_state_file() -> Path | None:
    for candidate in STATE_FILE_CANDIDATES:
        if candidate and candidate.exists():
            return candidate
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Show what would be cleared without writing")
    parser.add_argument("--confirm", action="store_true", help="Actually clear positions")
    parser.add_argument("--only-paper", action="store_true", default=True, help="Refuse to clear LIVE positions (default true)")
    args = parser.parse_args()

    if not args.dry_run and not args.confirm:
        print("Refusing to act without --dry-run or --confirm.", file=sys.stderr)
        sys.exit(2)

    state_file = find_state_file()
    if state_file is None:
        print("No state file found. Checked:", file=sys.stderr)
        for c in STATE_FILE_CANDIDATES:
            print(f"  - {c}", file=sys.stderr)
        sys.exit(1)

    with state_file.open() as f:
        state = json.load(f)

    positions = state.get("positions", state.get("open_positions", []))
    if not positions:
        print(f"No open positions in {state_file}.")
        return

    print(f"Found {len(positions)} open positions in {state_file}:")
    paper_count = live_count = 0
    for p in positions:
        mode = p.get("mode", "unknown")
        if mode.lower() == "paper":
            paper_count += 1
        elif mode.lower() == "live":
            live_count += 1
        print(f"  - {p.get('market_ticker', p.get('symbol', '?'))} "
              f"[{mode}] qty={p.get('quantity', '?')} entry={p.get('entry_price', '?')}")

    if live_count > 0 and args.only_paper:
        print(f"\n⚠️  {live_count} LIVE position(s) present. Refusing to clear.", file=sys.stderr)
        print("    Close LIVE positions through Kalshi UI first.", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print(f"\nDRY RUN — would clear {paper_count} paper positions. Re-run with --confirm.")
        return

    # Keep a backup before mutating.
    backup = state_file.with_suffix(state_file.suffix + ".bak")
    with backup.open("w") as f:
        json.dump(state, f, indent=2)

    if "positions" in state:
        state["positions"] = []
    if "open_positions" in state:
        state["open_positions"] = []

    with state_file.open("w") as f:
        json.dump(state, f, indent=2)

    print(f"Cleared {paper_count} paper positions. Backup saved to {backup}.")


if __name__ == "__main__":
    main()
