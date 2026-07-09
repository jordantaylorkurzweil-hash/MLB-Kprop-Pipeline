"""
mlb_results_puller.py — pulls actual final strikeout totals per starting
pitcher for a completed slate, straight from MLB Stats API. Free, no login.

Replaces the manually-supplied mlb_results_*_norm.json the old pipeline
expected Perplexity to fetch. Used by build_backtest.py to grade yesterday's
BF picks.

Usage:
    python mlb_results_puller.py 2026-07-08 --out results_2026-07-08.json

Output schema (list of dicts, one per starting pitcher who threw):
    [{"name": "Paul Skenes", "team": "PIT", "matchup": "CHC @ PIT",
      "actual_ks": 8, "ip": 6.0, "final_status": "Final"}, ...]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from datetime import datetime, timedelta

import httpx

STATS_BASE = "https://statsapi.mlb.com/api/v1"
FEED_BASE = "https://statsapi.mlb.com/api/v1.1"

TEAM_ABBR = {
    "Arizona Diamondbacks": "ARI", "Atlanta Braves": "ATL", "Baltimore Orioles": "BAL",
    "Boston Red Sox": "BOS", "Chicago Cubs": "CHC", "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE", "Colorado Rockies": "COL",
    "Detroit Tigers": "DET", "Houston Astros": "HOU", "Kansas City Royals": "KCR",
    "Los Angeles Angels": "LAA", "Los Angeles Dodgers": "LAD", "Miami Marlins": "MIA",
    "Milwaukee Brewers": "MIL", "Minnesota Twins": "MIN", "New York Mets": "NYM",
    "New York Yankees": "NYY", "Athletics": "ATH", "Oakland Athletics": "ATH",
    "Philadelphia Phillies": "PHI", "Pittsburgh Pirates": "PIT", "San Diego Padres": "SDP",
    "Seattle Mariners": "SEA", "San Francisco Giants": "SFG", "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TBR", "Texas Rangers": "TEX", "Toronto Blue Jays": "TOR",
    "Washington Nationals": "WSN",
}


def _get(url, params=None):
    r = httpx.get(url, params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_schedule(date_str):
    data = _get(f"{STATS_BASE}/schedule", {
        "sportId": 1, "date": date_str, "hydrate": "team",
    })
    games = []
    for d in data.get("dates", []):
        games.extend(d.get("games", []))
    return games


def _starter_line(team_box, team_abbr, opp_abbr, side_label):
    """Find the starting pitcher (first in 'pitchers' list) and their final line."""
    pitchers = team_box.get("pitchers") or []
    if not pitchers:
        return None
    starter_id = pitchers[0]
    p = (team_box.get("players") or {}).get(f"ID{starter_id}", {})
    stats = (p.get("stats") or {}).get("pitching") or {}
    if not stats:
        return None
    return {
        "name": p.get("person", {}).get("fullName"),
        "team": team_abbr,
        "matchup": f"{opp_abbr} @ {team_abbr}" if side_label == "home"
                   else f"{team_abbr} @ {opp_abbr}",
        "actual_ks": stats.get("strikeOuts"),
        "ip": stats.get("inningsPitched"),
    }


def pull_results(date_str):
    games = fetch_schedule(date_str)
    out = []
    for g in games:
        status = g.get("status", {}).get("detailedState", "")
        if "Final" not in status and "Completed" not in status:
            continue  # skip games not yet final
        game_pk = g["gamePk"]
        away_name = g["teams"]["away"]["team"]["name"]
        home_name = g["teams"]["home"]["team"]["name"]
        away_abbr = TEAM_ABBR.get(away_name, away_name[:3].upper())
        home_abbr = TEAM_ABBR.get(home_name, home_name[:3].upper())
        try:
            feed = fetch_live_feed(game_pk)
        except Exception as e:
            print(f"  ! feed fetch failed gamePk={game_pk}: {e}")
            continue
        box = feed.get("liveData", {}).get("boxscore", {}).get("teams", {})
        if not box:
            continue
        away_line = _starter_line(box.get("away", {}), away_abbr, home_abbr, "away")
        home_line = _starter_line(box.get("home", {}), home_abbr, away_abbr, "home")
        for line in (away_line, home_line):
            if line and line["name"] and line["actual_ks"] is not None:
                line["final_status"] = status
                out.append(line)
    return out


def fetch_live_feed(game_pk):
    return _get(f"{FEED_BASE}/game/{game_pk}/feed/live")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Pull actual starter K lines for a date (free, no LLM)")
    ap.add_argument("date", help="YYYY-MM-DD")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    results = pull_results(args.date)
    out_path = args.out or f"results_{args.date}.json"
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Wrote {len(results)} starter lines -> {out_path}")
