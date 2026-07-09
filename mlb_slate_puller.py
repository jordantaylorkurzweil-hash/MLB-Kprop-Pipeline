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
  - ppd_pct is a same-day precipitation-probability proxy from
