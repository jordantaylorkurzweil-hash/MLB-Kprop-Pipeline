"""
savant_pull.py — Baseball Savant data fetcher for the daily MLB K-prop model.

Fills the columns the FanGraphs free leaderboard doesn't expose:
    Whiff%, Hard-Hit%, Barrel%, xERA

Sources (via pybaseball):
  - statcast_pitcher_arsenal_stats: raw whiff_percent + hard_hit_percent per
    pitch type; we aggregate to a season-wide weighted average by pitch usage.
  - statcast_pitcher_expected_stats: season-wide xERA.
  - statcast_pitcher (barrel events): pulled lazily per-player only if caller
    asks, since it requires a Statcast date-range pull.

Usage:
    from savant_pull import get_savant_stats, lookup
    stats = get_savant_stats(season=2026, min_pa=25)
    row = lookup(stats, "Cristopher Sánchez")
    # row = {"whiff": float|None, "hard_hit": float|None,
    #        "barrel": float|None, "xera": float|None}

24-hour local cache at /home/user/workspace/repo/.savant_cache.json.
Leaves values as None when unavailable.
"""
from __future__ import annotations

import json
import os
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Optional

CACHE_PATH = Path(__file__).with_name(".savant_cache.json")
CACHE_TTL_SECONDS = 24 * 60 * 60


def _ascii(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def _norm_name(last: str, first: str) -> str:
    return f"{_ascii(last).strip().lower()}|{_ascii(first).strip().lower()}"


def _read_cache(season: int) -> Optional[Dict[str, Dict[str, Any]]]:
    if not CACHE_PATH.exists():
        return None
    try:
        blob = json.loads(CACHE_PATH.read_text())
    except json.JSONDecodeError:
        return None
    entry = blob.get(str(season))
    if not entry:
        return None
    if time.time() - entry.get("ts", 0) > CACHE_TTL_SECONDS:
        return None
    return entry.get("data")


def _write_cache(season: int, data: Dict[str, Dict[str, Any]]) -> None:
    blob: Dict[str, Any] = {}
    if CACHE_PATH.exists():
        try:
            blob = json.loads(CACHE_PATH.read_text())
        except json.JSONDecodeError:
            blob = {}
    blob[str(season)] = {"ts": time.time(), "data": data}
    CACHE_PATH.write_text(json.dumps(blob))


def get_savant_stats(
    season: int,
    min_pa: int = 25,
    force_refresh: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """
    Returns a dict keyed by 'lastname|firstname' (lowercased, ASCII).
    Value: {"whiff": %, "hard_hit": %, "barrel": None, "xera": value}

    Note: Barrel% requires a per-player Statcast date-range pull and is left
    as None here. Add to the daily pipeline if needed (~1 sec per pitcher).
    """
    if not force_refresh:
        cached = _read_cache(season)
        if cached is not None:
            return cached

    from pybaseball import (
        statcast_pitcher_arsenal_stats,
        statcast_pitcher_expected_stats,
    )

    out: Dict[str, Dict[str, Any]] = {}

    # 1) Weighted aggregate of Whiff% and Hard-Hit% across pitch arsenal
    try:
        arsenal = statcast_pitcher_arsenal_stats(season, minPA=min_pa)
        # Group by player; weight whiff_percent / hard_hit_percent by pitch_usage
        for (name_field, _pid), grp in arsenal.groupby(
            ["last_name, first_name", "player_id"]
        ):
            if "," in str(name_field):
                last, first = [p.strip() for p in str(name_field).split(",", 1)]
            else:
                last, first = str(name_field), ""
            k = _norm_name(last, first)

            # Weight by pitches (raw count) — more robust than pitch_usage %
            total_pitches = grp["pitches"].sum()
            if total_pitches and total_pitches > 0:
                whiff = (grp["whiff_percent"] * grp["pitches"]).sum() / total_pitches
                hh = (grp["hard_hit_percent"] * grp["pitches"]).sum() / total_pitches
            else:
                whiff = hh = None

            out.setdefault(k, {"whiff": None, "hard_hit": None,
                               "barrel": None, "xera": None})
            if whiff is not None:
                out[k]["whiff"] = round(float(whiff), 1)
            if hh is not None:
                out[k]["hard_hit"] = round(float(hh), 1)
    except Exception as e:
        print(f"[savant_pull] arsenal_stats fetch failed: {e}")

    # 2) Season xERA
    try:
        exp = statcast_pitcher_expected_stats(season, minPA=min_pa)
        for _, row in exp.iterrows():
            name_field = str(row.get("last_name, first_name", "")).strip()
            if "," in name_field:
                last, first = [p.strip() for p in name_field.split(",", 1)]
            else:
                last, first = name_field, ""
            k = _norm_name(last, first)
            out.setdefault(k, {"whiff": None, "hard_hit": None,
                               "barrel": None, "xera": None})
            xera = row.get("xera")
            if xera is not None and str(xera) != "nan":
                out[k]["xera"] = round(float(xera), 2)
    except Exception as e:
        print(f"[savant_pull] expected_stats fetch failed: {e}")

    if out:
        _write_cache(season, out)
    return out


def lookup(stats: Dict[str, Dict[str, Any]], full_name: str) -> Dict[str, Any]:
    """Convenience: look up by 'First Last' (case + accent insensitive)."""
    blank = {"whiff": None, "hard_hit": None, "barrel": None, "xera": None}
    parts = _ascii(full_name).replace(",", "").split()
    if len(parts) < 2:
        return blank
    first, last = parts[0], parts[-1]
    return stats.get(_norm_name(last, first), blank)


if __name__ == "__main__":
    import sys
    season = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
    print(f"[savant_pull] fetching season {season}...")
    s = get_savant_stats(season=season, force_refresh=True)
    print(f"[savant_pull] {len(s)} pitchers")
    # Spot-check a couple of names from today's slate
    for name in ["Cristopher Sánchez", "Kevin Gausman", "Logan Gilbert",
                 "Jacob deGrom", "Jose Soriano"]:
        r = lookup(s, name)
        print(f"  {name:25s}  Whiff={r['whiff']}  HardHit={r['hard_hit']}  xERA={r['xera']}")
