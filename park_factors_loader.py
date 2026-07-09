"""Park factor loader (v3.6.2).

Loads /home/user/workspace/repo/park_factors.json (Z-Files 2026 source) and
exposes a single function `park_k_shift(team_abbr, opp_lineup_dict)` that
returns the additive K shift in absolute Ks per start, lineup-weighted by the
opposing batter handedness (R/L/S counts from RotoWire).

Sign convention: positive = pitcher's Ks UP (K-inflating park).
"""

import json
import os

_PATH = os.path.join(os.path.dirname(__file__), "park_factors.json")
with open(_PATH) as _f:
    _DATA = json.load(_f)

TEAM_TO_PARK = _DATA["team_to_park"]
FACTORS_OVERALL = _DATA["factors_overall"]
FACTORS_RHB = _DATA["factors_RHB"]
FACTORS_LHB = _DATA["factors_LHB"]
FACTORS_SH  = _DATA["factors_SH"]
SOURCE = _DATA["source"]
AS_OF = _DATA["as_of"]
VERSION = _DATA["version"]


def park_for_team(team_abbr):
    """Map team abbreviation (home team) to park name."""
    return TEAM_TO_PARK.get(team_abbr)


def park_k_shift(team_abbr, opp_lineup=None):
    """Return absolute K shift for a pitcher in this park, weighted by opposing lineup.

    Args:
        team_abbr: home team abbrev (e.g. 'NYM').
        opp_lineup: dict like {'R':5, 'L':3, 'S':1}. If None or empty, returns overall.

    Returns:
        K shift in absolute Ks per start. Add directly to projected Ks.
    """
    park = park_for_team(team_abbr)
    if park is None:
        return 0.0
    if not opp_lineup:
        return FACTORS_OVERALL.get(park, 0.0)
    r = opp_lineup.get("R", 0)
    l = opp_lineup.get("L", 0)
    s = opp_lineup.get("S", 0)
    total = r + l + s
    if total == 0:
        return FACTORS_OVERALL.get(park, 0.0)
    f_r = FACTORS_RHB.get(park, 0.0)
    f_l = FACTORS_LHB.get(park, 0.0)
    f_s = FACTORS_SH.get(park, 0.0)
    return (f_r * r + f_l * l + f_s * s) / total


def overall(team_abbr):
    """Return non-lineup-weighted overall K shift for a team's home park."""
    park = park_for_team(team_abbr)
    return FACTORS_OVERALL.get(park, 0.0) if park else 0.0


if __name__ == "__main__":
    # Smoke test
    print(f"Loaded park_factors {VERSION} ({AS_OF}) — {len(FACTORS_OVERALL)} parks")
    print(f"Source: {SOURCE}\n")
    print("Sample lineup-weighted shifts (8R/1L/0S = mostly righty lineup):")
    for t in ["SEA","COL","STL","WSN","KCR","NYM","CIN","ATH"]:
        s_overall = overall(t)
        s_weighted = park_k_shift(t, {"R":8, "L":1, "S":0})
        print(f"  {t}: overall {s_overall:+.2f} | vs 8R/1L/0S {s_weighted:+.2f}")
