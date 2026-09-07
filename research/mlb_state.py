"""
MLB game-state anchors from the public MLB Stats API (no key).

Gives K1 a LIVE-IMPLEMENTABLE definition of "final minutes": the start of the
9th inning (first play of the top of the 9th), instead of "N minutes before
Kalshi's close", which is only known after the fact.

    schedule(date_et)             -> [{gamePk, away, home, start_utc}]
    inning_anchors(gamePk)        -> {"top9_start": ts, "bot9_start": ts|None,
                                      "last_play": ts, "innings": n}
    kalshi_event_to_game(event)   -> (date_et, away, home, sched_ts)
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

MLB = "https://statsapi.mlb.com/api/v1"
ET = ZoneInfo("America/New_York")
_EV = re.compile(r"^KXMLBGAME-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})([A-Z]+)$")


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "rudebot-research/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:  # noqa: BLE001
            time.sleep(1.0 * (attempt + 1))
    return None


@lru_cache(maxsize=None)
def team_abbrevs() -> tuple[str, ...]:
    d = _get(f"{MLB}/teams?sportId=1") or {}
    return tuple(sorted((t["abbreviation"] for t in d.get("teams", [])), key=len, reverse=True))


def kalshi_event_to_game(event: str):
    """'KXMLBGAME-26SEP061610NYYSD' -> ('2026-09-06', 'NYY', 'SD', sched_ts)."""
    m = _EV.match(event)
    if not m:
        return None
    yy, mon, dd, hh, mm, teams = m.groups()
    dt = datetime(2000 + int(yy), datetime.strptime(mon, "%b").month, int(dd), int(hh), int(mm), tzinfo=ET)
    for a in team_abbrevs():
        if teams.startswith(a) and teams[len(a):] in team_abbrevs():
            return dt.strftime("%Y-%m-%d"), a, teams[len(a):], int(dt.timestamp())
    return None


@lru_cache(maxsize=None)
def schedule(date_et: str) -> list[dict]:
    d = _get(f"{MLB}/schedule?sportId=1&date={date_et}") or {}
    out = []
    for day in d.get("dates", []):
        for g in day.get("games", []):
            out.append({"gamePk": g["gamePk"],
                        "away": g["teams"]["away"]["team"].get("abbreviation") or "",
                        "home": g["teams"]["home"]["team"].get("abbreviation") or "",
                        "start_utc": g.get("gameDate"), "state": g.get("status", {}).get("detailedState")})
    if not out or not out[0]["away"]:
        # abbreviations are not in the schedule payload: hydrate via teams map
        abbr = {t["id"]: t["abbreviation"] for t in (_get(f"{MLB}/teams?sportId=1") or {}).get("teams", [])}
        for day in d.get("dates", []):
            for g, o in zip(day.get("games", []), out):
                o["away"] = abbr.get(g["teams"]["away"]["team"]["id"], "")
                o["home"] = abbr.get(g["teams"]["home"]["team"]["id"], "")
    return out


def find_game(event: str):
    info = kalshi_event_to_game(event)
    if not info:
        return None
    date_et, away, home, sched_ts = info
    cands = [g for g in schedule(date_et) if g["away"] == away and g["home"] == home]
    if not cands:
        return None
    if len(cands) > 1:   # doubleheader: nearest scheduled start
        cands.sort(key=lambda g: abs(int(datetime.fromisoformat(g["start_utc"].replace("Z", "+00:00")).timestamp()) - sched_ts))
    return cands[0]


def _ts(iso: str | None):
    if not iso:
        return None
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


@lru_cache(maxsize=None)
def inning_anchors(game_pk: int) -> dict | None:
    f = _get(f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live")
    if not f:
        return None
    plays = (f.get("liveData", {}).get("plays", {}) or {}).get("allPlays", []) or []
    if not plays:
        return None
    top9 = bot9 = None
    for p in plays:
        ab = p.get("about", {})
        if ab.get("inning") == 9 and ab.get("halfInning") == "top" and top9 is None:
            top9 = _ts(ab.get("startTime"))
        if ab.get("inning") == 9 and ab.get("halfInning") == "bottom" and bot9 is None:
            bot9 = _ts(ab.get("startTime"))
    last = _ts(plays[-1].get("about", {}).get("endTime"))
    return {"top9_start": top9, "bot9_start": bot9, "last_play": last,
            "innings": plays[-1].get("about", {}).get("inning"),
            "final_state": f.get("gameData", {}).get("status", {}).get("detailedState")}


def live_state(game_pk: int) -> dict | None:
    """For the bot: current inning / half / outs / score (linescore)."""
    d = _get(f"{MLB}/game/{game_pk}/linescore")
    if not d:
        return None
    return {"inning": d.get("currentInning"), "half": d.get("inningState"), "outs": d.get("outs"),
            "away_runs": (d.get("teams", {}).get("away", {}) or {}).get("runs"),
            "home_runs": (d.get("teams", {}).get("home", {}) or {}).get("runs"),
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
