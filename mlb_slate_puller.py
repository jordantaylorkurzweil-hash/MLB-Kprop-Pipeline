"""
mlb_slate_puller.py — replaces the Perplexity/RotoWire agent step.

Pulls today's (or a given date's) MLB slate — probable starters, confirmed
lineups (once posted), park, and weather/rain-risk — entirely from free,
public sources:

  - MLB Stats API (statsapi.mlb.com)   → schedule, probable pitchers,
                                          venue, confirmed lineups, weather
  - Open-Meteo (open-meteo.com)        → precipitation probability at
                                          first pitch, used as a PPD% proxy
                                          for outdoor parks (no API key)

No login, no scraping, no LLM. Writes a JSON file in the exact schema
run_model.py expects:

    {"games": [
        {
          "away_team": "Chicago Cubs", "home_team": "Pittsburgh Pirates",
          "away_sp": {"name": "...", "throws": "L"},
          "home_sp": {"name": "...", "throws": "R"},
          "away_lineup": [{"bats": "R"}, ...],   # AWAY team's own batters
          "home_lineup": [{"bats": "L"}, ...],   # HOME team's own batters
          "away_lineup_status": "CONFIRMED" | "PROJECTED",
          "home_lineup_status": "CONFIRMED" | "PROJECTED",
          "first_pitch_et": "2026-07-09T19:05:00-04:00",
          "park": "PNC Park", "park_team": "PIT",
          "weather": "72F, Partly Cloudy",
          "ppd_pct": 12, "indoor": false,
          "status": "Scheduled"
        }, ...
    ]}

Usage:
    python mlb_slate_puller.py 2026-07-09 --late-after 19:00 --out /path/to/slate.json

NOTE ON LIMITS vs. the old RotoWire pull:
  - Confirmed lineups are only available once MLB posts them (usually
    1-2 hours before first pitch). Earlier than that, lineup fields come
    back empty and *_lineup_status = "PROJECTED" — the model already
    falls back to non-lineup-weighted park factors in that case, same as
    it did when RotoWire hadn't posted lineups yet.
  - ppd_pct is a same-day precipitation-probability proxy from Open-Meteo,
    not RotoWire's proprietary postponement number. Treat it as directional,
    not identical — worth a gut-check against a live line the first few
    times you run this.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from park_coords import PARK_COORDS, INDOOR_OR_RETRACTABLE

STATS_BASE = "https://statsapi.mlb.com/api/v1"
FEED_BASE = "https://statsapi.mlb.com/api/v1.1"
OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"

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
        "sportId": 1,
        "date": date_str,
        "hydrate": "team,venue,probablePitcher,linescore",
    })
    games = []
    for d in data.get("dates", []):
        games.extend(d.get("games", []))
    return games


def fetch_live_feed(game_pk):
    return _get(f"{FEED_BASE}/game/{game_pk}/feed/live")


def _lineup_from_boxscore(team_box):
    """Return (list_of_{'bats':X}, status) for a team's boxscore block."""
    order = team_box.get("battingOrder") or []
    players = team_box.get("players") or {}
    if not order:
        return [], "PROJECTED"
    lineup = []
    for pid in order:
        p = players.get(f"ID{pid}", {})
        bat_side = (p.get("person", {}).get("batSide", {}) or {}).get("code")
        if bat_side:
            lineup.append({"bats": bat_side})
    if len(lineup) < len(order):
        # partial data — still treat what we got as confirmed, rest unknown
        pass
    return lineup, ("CONFIRMED" if lineup else "PROJECTED")


def _ppd_pct(park_abbr, first_pitch_utc):
    """Rough precipitation-probability proxy via Open-Meteo. Returns 0-100 int."""
    if park_abbr in INDOOR_OR_RETRACTABLE:
        return 0
    coords = PARK_COORDS.get(park_abbr)
    if not coords:
        return 0
    lat, lon = coords
    try:
        data = _get(OPEN_METEO_BASE, {
            "latitude": lat, "longitude": lon,
            "hourly": "precipitation_probability",
            "forecast_days": 3,
            "timezone": "America/New_York",
        })
        times = data.get("hourly", {}).get("time", [])
        probs = data.get("hourly", {}).get("precipitation_probability", [])
        target = first_pitch_utc.strftime("%Y-%m-%dT%H:00")
        if target in times:
            idx = times.index(target)
            return int(probs[idx])
    except Exception as e:
        print(f"  ! weather lookup failed for {park_abbr}: {e}")
    return 0


def build_slate(date_str, late_after=None):
    """late_after: 'HH:MM' ET string — if given, only include games with
    first pitch at/after that time (for the 5:30pm late-slate refresh)."""
    games_raw = fetch_schedule(date_str)
    out_games = []

    for g in games_raw:
        game_pk = g["gamePk"]
        away = g["teams"]["away"]["team"]["name"]
        home = g["teams"]["home"]["team"]["name"]
        venue = g.get("venue", {}).get("name", "")
        status = g.get("status", {}).get("detailedState", "Scheduled")
        game_dt_utc = datetime.fromisoformat(g["gameDate"].replace("Z", "+00:00"))
        game_dt_et = game_dt_utc - timedelta(hours=4)  # approx ET; DST-era season

        if late_after:
            hh, mm = map(int, late_after.split(":"))
            if (game_dt_et.hour, game_dt_et.minute) < (hh, mm):
                continue

        away_sp = g["teams"]["away"].get("probablePitcher") or {}
        home_sp = g["teams"]["home"].get("probablePitcher") or {}
        home_abbr = TEAM_ABBR.get(home, home[:3].upper())

        # Defaults if live feed fetch fails or lineups aren't posted
        away_lineup, away_status = [], "PROJECTED"
        home_lineup, home_status = [], "PROJECTED"
        weather_str = ""
        indoor = home_abbr in INDOOR_OR_RETRACTABLE

        try:
            feed = fetch_live_feed(game_pk)
            box = feed.get("liveData", {}).get("boxscore", {}).get("teams", {})
            if box:
                away_lineup, away_status = _lineup_from_boxscore(box.get("away", {}))
                home_lineup, home_status = _lineup_from_boxscore(box.get("home", {}))
            wx = feed.get("gameData", {}).get("weather") or {}
            if wx:
                parts = []
                if wx.get("temp"):
                    parts.append(f"{wx['temp']}F")
                if wx.get("condition"):
                    parts.append(wx["condition"])
                if wx.get("wind"):
                    parts.append(f"Wind {wx['wind']}")
                weather_str = ", ".join(parts)
        except Exception as e:
            print(f"  ! live feed fetch failed for gamePk={game_pk}: {e}")

        # away_sp / home_sp names — probablePitcher gives fullName + pitchHand
        def _sp(entry):
            if not entry:
                return {"name": None, "throws": "R"}
            return {
                "name": entry.get("fullName"),
                "throws": (entry.get("pitchHand", {}) or {}).get("code", "R"),
            }

        ppd = _ppd_pct(home_abbr, game_dt_utc)

        out_games.append({
            "away_team": away,
            "home_team": home,
            "away_sp": _sp(away_sp),
            "home_sp": _sp(home_sp),
            "away_lineup": away_lineup,
            "home_lineup": home_lineup,
            "away_lineup_status": away_status,
            "home_lineup_status": home_status,
            "first_pitch_et": game_dt_et.isoformat(),
            "park": venue,
            "park_team": home_abbr,
            "weather": weather_str,
            "ppd_pct": ppd,
            "indoor": indoor,
            "status": status,
        })

    return {"games": out_games}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Pull MLB slate (free, no LLM/scrape)")
    ap.add_argument("date", help="YYYY-MM-DD (ET slate date)")
    ap.add_argument("--late-after", default=None,
                     help="HH:MM ET — only include games starting at/after this time")
    ap.add_argument("--out", default=None, help="Output JSON path")
    args = ap.parse_args()

    slate = build_slate(args.date, late_after=args.late_after)
    out_path = args.out or f"slate_{args.date}.json"
    with open(out_path, "w") as f:
        json.dump(slate, f, indent=2)
    print(f"Wrote {len(slate['games'])} games -> {out_path}")
