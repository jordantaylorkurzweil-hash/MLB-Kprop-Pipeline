"""
The Odds API loader for MLB pitcher strikeout props.

Fetches lines + juice from DraftKings and FanDuel via api.the-odds-api.com.
Replaces the scrape-based browser_task path which was blocked by login walls (DK)
and Kasada (FD).

Usage:
    from odds_api_loader import get_pitcher_strikeouts
    lines = get_pitcher_strikeouts(date="2026-05-28")
    # → {"Paul Skenes": {"DK": {"line": 6.5, "over": -112, "under": -114},
    #                    "FD": {"line": 6.5, "over": 100, "under": -122}}}

Auth: reads the ODDS_API_KEY environment variable and appends it as the
`apiKey` query param on every request. Set it as a GitHub Actions secret
(or local env var) — get a free-tier key at https://the-odds-api.com
(500 requests/month free, and this pipeline uses ~1-2/day per run).

Cache: per-day JSON file in <this dir>/.odds_cache_{date}.json
       TTL 30 min — refresh by deleting the file.
"""
import json, os, time, unicodedata
import httpx
from pathlib import Path
from datetime import datetime

BASE = "https://api.the-odds-api.com/v4"
SPORT = "baseball_mlb"
CACHE_DIR = Path(__file__).parent
CACHE_TTL_SEC = 30 * 60  # 30 minutes
API_KEY_ENV = "ODDS_API_KEY"


def _norm_name(name):
    """Normalize for matching (strip diacritics, lowercase, collapse spaces)."""
    s = "".join(c for c in unicodedata.normalize("NFD", name or "")
                if unicodedata.category(c) != "Mn")
    return " ".join(s.strip().split())


def _http_get(path, params=None):
    """GET via httpx, with the API key injected from ODDS_API_KEY."""
    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        raise RuntimeError(
            f"{API_KEY_ENV} environment variable not set. Get a free key at "
            f"https://the-odds-api.com and set it as a repo secret / env var."
        )
    url = f"{BASE}{path}"
    merged = dict(params or {})
    merged["apiKey"] = api_key
    r = httpx.get(url, params=merged, timeout=30)
    r.raise_for_status()
    return r.json()


def list_events(date=None):
    """List MLB events. If date='YYYY-MM-DD', filter to that ET day.

    Note: commence_time is UTC. ET = UTC-4 (DST) / UTC-5 (standard).
    For a US date filter we accept any commence_time on that UTC date OR
    the next UTC date up to ~04:00 UTC (covers late ET games).
    """
    events = _http_get(f"/sports/{SPORT}/events", {"dateFormat": "iso"})
    if not date:
        return events
    # Accept anything whose ET-equivalent date matches
    from datetime import datetime as _dt, timedelta
    target = _dt.strptime(date, "%Y-%m-%d").date()
    out = []
    for e in events:
        ct = e.get("commence_time", "")
        try:
            utc_dt = _dt.fromisoformat(ct.replace("Z", "+00:00"))
            # ET is UTC-4 during DST (May)
            et_date = (utc_dt - timedelta(hours=4)).date()
            if et_date == target:
                out.append(e)
        except Exception:
            continue
    return out


def fetch_event_odds(event_id, markets="pitcher_strikeouts",
                    bookmakers="draftkings,fanduel"):
    """Fetch odds for one event from specified bookmakers/markets.

    NOTE: Player props (pitcher_strikeouts) are ONLY available via this
    per-event endpoint — the bulk /odds endpoint returns INVALID_MARKET.
    """
    params = {
        "regions": "us",
        "markets": markets,
        "oddsFormat": "american",
        "bookmakers": bookmakers,
    }
    return _http_get(f"/sports/{SPORT}/events/{event_id}/odds", params)


def fetch_bulk_odds(markets="pitcher_strikeouts",
                   bookmakers="draftkings,fanduel"):
    """Fetch odds for ALL upcoming MLB events in ONE call (bulk endpoint).

    Costs 10 credits total (not 10 × N games), which is ~15x cheaper than
    fetch_event_odds() looped over a 15-game slate. Returns the same per-event
    shape as the event endpoint, just wrapped in an array.
    """
    params = {
        "regions": "us",
        "markets": markets,
        "oddsFormat": "american",
        "bookmakers": bookmakers,
        "dateFormat": "iso",
    }
    return _http_get(f"/sports/{SPORT}/odds", params)


def get_pitcher_strikeouts(date=None, use_cache=True):
    """Return dict keyed by normalized pitcher name with DK + FD lines & juice.

    Schema:
      {
        "<normalized name>": {
          "display_name": "Paul Skenes",
          "matchup": "CHC @ PIT",
          "DK": {"line": 6.5, "over": -112, "under": -114},
          "FD": {"line": 6.5, "over": 100,  "under": -122}
        },
        ...
      }
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    cache = CACHE_DIR / f".odds_cache_{date}.json"
    if use_cache and cache.exists():
        age = time.time() - cache.stat().st_mtime
        if age < CACHE_TTL_SEC:
            return json.load(open(cache))

    # NOTE: pitcher_strikeouts is a PLAYER prop market — The Odds API only
    # serves player props via the per-event endpoint (/events/{id}/odds).
    # The bulk /odds endpoint returns 422 INVALID_MARKET for player props.
    # So we must loop one call per game (10 credits each).
    events = list_events(date=date)
    result = {}
    for e in events:
        home = e.get("home_team", "")
        away = e.get("away_team", "")
        matchup = f"{_team_abbr(away)} @ {_team_abbr(home)}"
        try:
            ev_odds = fetch_event_odds(e["id"])
        except Exception as ex:
            print(f"  ! Failed odds for {matchup}: {ex}")
            continue
        for bm in ev_odds.get("bookmakers", []):
            book = bm["key"]  # 'draftkings' or 'fanduel'
            book_short = "DK" if book == "draftkings" else "FD"
            for mk in bm.get("markets", []):
                if mk["key"] != "pitcher_strikeouts":
                    continue
                # Outcomes come in Over/Under pairs per pitcher
                by_pitcher = {}
                for o in mk.get("outcomes", []):
                    pname = o.get("description", "")
                    side = o.get("name", "").lower()  # 'over' or 'under'
                    by_pitcher.setdefault(pname, {})["line"] = o.get("point")
                    by_pitcher[pname][side] = o.get("price")
                for pname, vals in by_pitcher.items():
                    key = _norm_name(pname)
                    if key not in result:
                        result[key] = {"display_name": pname, "matchup": matchup}
                    result[key][book_short] = {
                        "line": vals.get("line"),
                        "over": vals.get("over"),
                        "under": vals.get("under"),
                    }

    with open(cache, "w") as f:
        json.dump(result, f, indent=2)
    return result


# Best-effort team abbreviation mapping (Odds API uses full names)
_TEAM_ABBR = {
    "Los Angeles Angels":"LAA","Detroit Tigers":"DET","Minnesota Twins":"MIN",
    "Chicago White Sox":"CWS","Atlanta Braves":"ATL","Boston Red Sox":"BOS",
    "Toronto Blue Jays":"TOR","Baltimore Orioles":"BAL","Chicago Cubs":"CHC",
    "Pittsburgh Pirates":"PIT","Houston Astros":"HOU","Texas Rangers":"TEX",
    "New York Yankees":"NYY","New York Mets":"NYM","Tampa Bay Rays":"TBR",
    "Los Angeles Dodgers":"LAD","San Diego Padres":"SDP","San Francisco Giants":"SFG",
    "Philadelphia Phillies":"PHI","Washington Nationals":"WSN","Miami Marlins":"MIA",
    "Cincinnati Reds":"CIN","St. Louis Cardinals":"STL","Milwaukee Brewers":"MIL",
    "Kansas City Royals":"KCR","Cleveland Guardians":"CLE","Seattle Mariners":"SEA",
    "Oakland Athletics":"OAK","Athletics":"OAK","Colorado Rockies":"COL","Arizona Diamondbacks":"ARI",
}


def _team_abbr(full):
    return _TEAM_ABBR.get(full, full[:3].upper())


def best_under_juice(pitcher_data, threshold=-130):
    """Return (gate_price, book) — the TIGHTEST Under price that still qualifies
    the BF gate (i.e. most negative price ≤ threshold).

    Semantics (BF-gate-aware):
      The BF gate asks 'does ANY book offer Under juice ≤ -130?'.
      So we look for the most-negative price across DK + FD that meets the threshold.
      If both books are softer than threshold (e.g. DK -124, FD -128), we return the
      *closest* to threshold (FD -128) and the gate will reject it.
      If at least one book is at/below threshold, that book's price is returned and
      the gate fires.

    Returns (None, None) if no Under price available.
    """
    candidates = []
    for book in ("DK", "FD"):
        d = pitcher_data.get(book, {})
        u = d.get("under")
        if u is not None:
            candidates.append((u, book))
    if not candidates:
        return None, None
    # Prefer the most-negative price that meets the threshold;
    # if none meets it, return the closest to threshold (least-favorable for the gate).
    qualifying = [c for c in candidates if c[0] <= threshold]
    if qualifying:
        # most negative wins (e.g. -150 beats -130)
        return min(qualifying, key=lambda x: x[0])
    # Nothing qualifies — return the price closest to threshold (max, i.e. least negative below 0)
    # so the caller can still see how far away we are.
    return max(candidates, key=lambda x: x[0])


def best_under_for_bettor(pitcher_data):
    """Return (best_price, book) = the price most favorable to the bettor.

    Use this for the 'what would I actually pay if I shopped' display column.
    For Under -130 vs Under -110, -110 is better (less juice paid) — so max() wins.
    """
    candidates = []
    for book in ("DK", "FD"):
        d = pitcher_data.get(book, {})
        u = d.get("under")
        if u is not None:
            candidates.append((u, book))
    if not candidates:
        return None, None
    return max(candidates, key=lambda x: x[0])


if __name__ == "__main__":
    print(f"Pulling MLB strikeout props for today...")
    data = get_pitcher_strikeouts()
    print(f"\nGot {len(data)} pitchers:\n")
    for key, d in sorted(data.items()):
        dk = d.get("DK", {})
        fd = d.get("FD", {})
        gp, gb = best_under_juice(d)
        sp, sb = best_under_for_bettor(d)
        print(f"  {d['display_name']:<22} ({d['matchup']:<12}) | "
              f"DK {dk.get('line')}/{dk.get('over')}/{dk.get('under')} | "
              f"FD {fd.get('line')}/{fd.get('over')}/{fd.get('under')} | "
              f"Gate: {gp} ({gb}) | Shop: {sp} ({sb})")
