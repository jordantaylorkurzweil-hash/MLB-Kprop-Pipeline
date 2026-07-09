"""Daily MLB K-prop runner — portable version (v3.6.2 model logic unchanged).

Changes vs the original Perplexity-agent runner:
- Parameterized by --date and --slate (am/late) instead of one hardcoded
  script per day. Run with: python run_model.py 2026-07-09 --slate am
- Slate now comes from mlb_slate_puller.py (free MLB Stats API) instead of
  a RotoWire JSON the agent had to scrape.
- Odds now fetched directly via get_pitcher_strikeouts() instead of reading
  a separately-dumped odds_*.json — one less manual step.
- All paths route through repo/_paths.py (WORKSPACE) instead of the
  hardcoded /home/user/workspace sandbox path.

Model logic below (classification, multipliers, Rule 5, park/platoon
adjustment, v3.5/v4.0 projections, BF gate) is UNCHANGED from the original.
"""
import argparse
import os
import csv
import json
import unicodedata
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "repo"))

from _paths import WORKSPACE
from fangraphs_loader import load_fg_csv
from odds_api_loader import get_pitcher_strikeouts, best_under_juice, best_under_for_bettor
from kprop_v35_core import v33_calibrate
from kprop_v40_core import predict_v40
from park_factors_loader import park_k_shift, overall as park_overall, AS_OF as PF_AS_OF

ap = argparse.ArgumentParser(description="Run the MLB K-prop model for a given slate")
ap.add_argument("date", nargs="?", default=datetime.now().strftime("%Y-%m-%d"),
                 help="YYYY-MM-DD (defaults to today)")
ap.add_argument("--slate", choices=["am", "late"], default="am",
                 help="am = full slate (11am run), late = late-slate refresh (5:30pm run)")
ap.add_argument("--slate-file", default=None,
                 help="Path to slate JSON from mlb_slate_puller.py (default: WORKSPACE/slate_<date>_<slate>.json)")
_args = ap.parse_args()
DATE_STR = _args.date
SLATE_TYPE = _args.slate


# ── Slate (loaded from RotoWire JSON) ─────────────────────────────────────────
def _ascii(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()

def norm_name(n):
    return " ".join(_ascii(n).strip().split())

# Aliases: RotoWire abbreviated first-initial names -> Odds API / FG full names
NAME_ALIAS = {
    "j misiorowski": "jacob misiorowski",
    "j. misiorowski": "jacob misiorowski",
    "s arrighetti": "spencer arrighetti",
    "s. arrighetti": "spencer arrighetti",
    "sam aldegheri": "samuel aldegheri",
    "sam. aldegheri": "samuel aldegheri",
}

def _strip_dots(k):
    return k.replace(".", "").replace("  ", " ").strip()

def alias_key(name):
    k = norm_name(name).lower()
    if k in NAME_ALIAS:
        return NAME_ALIAS[k]
    k2 = _strip_dots(k)
    if k2 in NAME_ALIAS:
        return NAME_ALIAS[k2]
    return k2 if k2 != k else k

# Team name → abbreviation (for park lookup)
TEAM_ABBR = {
    "Arizona Diamondbacks":"ARI","Atlanta Braves":"ATL","Baltimore Orioles":"BAL",
    "Boston Red Sox":"BOS","Chicago Cubs":"CHC","Chicago White Sox":"CWS",
    "Cincinnati Reds":"CIN","Cleveland Guardians":"CLE","Colorado Rockies":"COL",
    "Detroit Tigers":"DET","Houston Astros":"HOU","Kansas City Royals":"KCR",
    "Los Angeles Angels":"LAA","Los Angeles Dodgers":"LAD","Miami Marlins":"MIA",
    "Milwaukee Brewers":"MIL","Minnesota Twins":"MIN","New York Mets":"NYM",
    "New York Yankees":"NYY","Athletics":"OAK","Oakland Athletics":"OAK",
    "Philadelphia Phillies":"PHI","Pittsburgh Pirates":"PIT","San Diego Padres":"SDP",
    "Seattle Mariners":"SEA","San Francisco Giants":"SFG","St. Louis Cardinals":"STL",
    "Tampa Bay Rays":"TBR","Texas Rangers":"TEX","Toronto Blue Jays":"TOR",
    "Washington Nationals":"WSN",
}

# Classification multipliers (v3.5)
MULTIPLIERS = {
    "SwStr-Dominant": 0.95, "Above-Zone": 0.93, "Below-Zone": 0.92,
    "Mixed": 0.91, "CS-Dependent": 0.90,
}
KNOWN_CLASSES = {
    "jose soriano":"SwStr-Dominant","mackenzie gore":"Mixed",
    "david peterson":"CS-Dependent","brady singer":"CS-Dependent",
    "george kirby":"Mixed",
}

def classify(name, swstr, whiff):
    nn = norm_name(name).lower()
    if nn in KNOWN_CLASSES:
        return KNOWN_CLASSES[nn], f"Known: {KNOWN_CLASSES[nn]}"
    if swstr is not None and swstr >= 11.0:
        return "Mixed", "SwStr%>=11"
    if whiff is not None and whiff >= 27.0:
        return "Mixed", "Whiff%>=27"
    if swstr is not None and swstr >= 13.0:
        return "SwStr-Dominant", "SwStr%>=13"
    return "Mixed", "Default"


# ── Savant cache wrapper ──────────────────────────────────────────────────────
_SAVANT_CACHE_PATH = Path(__file__).resolve().parent.parent / "repo" / ".savant_cache.json"
if _SAVANT_CACHE_PATH.exists():
    _SAVANT = json.load(open(_SAVANT_CACHE_PATH))["2026"]["data"]
else:
    print("[run_model] No .savant_cache.json yet — run savant_pull.py first. Continuing with empty cache.")
    _SAVANT = {}

def savant_lookup(name):
    parts = alias_key(name).split()
    if len(parts) < 2:
        return {}
    last, first = parts[-1], " ".join(parts[:-1])
    # try a few key shapes
    for k in [f"{last}|{first}", f"{last}|{first.replace(' ', '')}",
              f"{last}|{'.'.join(first.replace('.','')) }."]:
        if k in _SAVANT:
            return _SAVANT[k]
    # JT Ginn → ginn|j.t. — try inserting dots
    if len(first) <= 3 and "." not in first:
        dotted = ".".join(list(first)) + "."
        k = f"{last}|{dotted}"
        if k in _SAVANT:
            return _SAVANT[k]
    return {}


# ── FG lookup wrapper ─────────────────────────────────────────────────────────
def fg_lookup(fg, name):
    nn = norm_name(name).lower()
    hit = fg.get(nn)
    if hit:
        return hit
    ak = alias_key(name)
    if ak != nn:
        return fg.get(ak)
    return None


# ── Env adjustment with v3.6.2 park factors ───────────────────────────────────
def env_adjust(park_team, opp_lu, hand):
    """Park-K shift (v3.6.2, lineup-weighted) + opp handedness platoon adjustment."""
    park_shift = park_k_shift(park_team, opp_lu)  # absolute Ks (already scaled)
    total = opp_lu["R"] + opp_lu["L"] + opp_lu["S"]
    if total == 0:
        return park_shift
    if hand == "L":
        right_pct = opp_lu["R"] / total
        plat = (right_pct - 0.55) * 0.6
    else:
        left_pct = opp_lu["L"] / total
        plat = (left_pct - 0.45) * 0.4
    return park_shift + plat


# ── Load slate from RotoWire JSON ─────────────────────────────────────────────
# ── Load slate from mlb_slate_puller.py output ────────────────────────────────
SLATE_PATH = _args.slate_file or str(WORKSPACE / f"slate_{DATE_STR}_{SLATE_TYPE}.json")
if not os.path.exists(SLATE_PATH):
    raise SystemExit(
        f"Slate file not found: {SLATE_PATH}\n"
        f"Run: python scripts/mlb_slate_puller.py {DATE_STR} --out {SLATE_PATH}"
        + (" --late-after 19:00" if SLATE_TYPE == "late" else "")
    )

with open(SLATE_PATH) as f:
    rw = json.load(f)

def lineup_dict(lu_list):
    def bats_of(b):
        if isinstance(b, dict): return b.get("bats", "")
        return ""
    R = sum(1 for b in lu_list if bats_of(b) == "R")
    L = sum(1 for b in lu_list if bats_of(b) == "L")
    S = sum(1 for b in lu_list if bats_of(b) == "S")
    return {"R": R, "L": L, "S": S, "confirmed": True}

SLATE = []
for g in (rw["games"] if isinstance(rw, dict) and "games" in rw else rw):
    away = g["away_team"]; home = g["home_team"]
    away_abbr = TEAM_ABBR.get(away, away[:3].upper())
    home_abbr = TEAM_ABBR.get(home, home[:3].upper())
    away_lu = g.get("away_lineup", []) or []
    home_lu = g.get("home_lineup", []) or []
    # away_opp_hand = handedness counts the AWAY SP will face (i.e. HOME lineup composition)
    # home_opp_hand = counts the HOME SP will face (i.e. AWAY lineup composition)
    away_opp = g.get("away_opp_hand") or {}  # what AWAY SP faces (= HOME lineup)
    home_opp = g.get("home_opp_hand") or {}  # what HOME SP faces (= AWAY lineup)
    if not away_lu and home_opp:
        # Aggregated counts present — synthesize a list of {bats} so lineup_dict works downstream.
        away_lu = [{"bats": "R"} for _ in range(int(home_opp.get("R", 0)))] + \
                  [{"bats": "L"} for _ in range(int(home_opp.get("L", 0)))] + \
                  [{"bats": "S"} for _ in range(int(home_opp.get("S", 0)))]
    if not home_lu and away_opp:
        home_lu = [{"bats": "R"} for _ in range(int(away_opp.get("R", 0)))] + \
                  [{"bats": "L"} for _ in range(int(away_opp.get("L", 0)))] + \
                  [{"bats": "S"} for _ in range(int(away_opp.get("S", 0)))]
    SLATE.append({
        "matchup": f"{away_abbr} @ {home_abbr}",
        "time": g.get("first_pitch_et", ""),
        "park": g.get("park", ""),
        "park_team": home_abbr,
        "weather": g.get("weather", "") if isinstance(g.get("weather"), str) else (g.get("weather", {}) or {}).get("conditions", ""),
        "ppd_pct": g.get("ppd_pct", 0),
        "indoor": bool(g.get("indoor", False)) or (isinstance(g.get("weather"), dict) and bool(g.get("weather", {}).get("dome"))) or "dome" in str(g.get("weather", "") or "").lower() or "indoor" in str(g.get("weather", "") or "").lower(),
        "status": g.get("status", "scheduled"),
        "away_sp": {"name": g["away_sp"]["name"] if isinstance(g["away_sp"], dict) else g["away_sp"], "team": away_abbr,
                    "hand": (g["away_sp"].get("throws") or g["away_sp"].get("hand") or "R") if isinstance(g["away_sp"], dict) else g.get("away_sp_hand", "R")},
        "home_sp": {"name": g["home_sp"]["name"] if isinstance(g["home_sp"], dict) else g["home_sp"], "team": home_abbr,
                    "hand": (g["home_sp"].get("throws") or g["home_sp"].get("hand") or "R") if isinstance(g["home_sp"], dict) else g.get("home_sp_hand", "R")},
        "away_lu": {**lineup_dict(away_lu), "confirmed": (g.get("away_lineup_status","").upper() == "CONFIRMED") or bool(g.get("away_lineup_confirmed"))},
        "home_lu": {**lineup_dict(home_lu), "confirmed": (g.get("home_lineup_status","").upper() == "CONFIRMED") or bool(g.get("home_lineup_confirmed"))},
        "notes": g.get("notes", ""),
    })

print(f"Loaded slate: {len(SLATE)} games")
print(f"Park factors source: {PF_AS_OF}")

# ── Load FG + lines ───────────────────────────────────────────────────────────
print("\nLoading FG cohort...")
fg = load_fg_csv()
print(f"  {len(fg)} pitchers in cohort")

print("\nLoading lines...")
_lines_raw = get_pitcher_strikeouts(date=DATE_STR)
# get_pitcher_strikeouts() shape: {norm_name: {display_name, matchup, DK:{...}, FD:{...}}}
lines_by_name = {}
for _key_raw, _v in _lines_raw.items():
    key = _key_raw.lower()
    _dk = _v.get("DK") or {}
    _fd = _v.get("FD") or {}
    lines_by_name[key] = {
        "display_name": _v.get("display_name", _key_raw),
        "team": _v.get("team", ""),
        "DK": {"line": _dk.get("line"), "over": _dk.get("over"), "under": _dk.get("under")},
        "FD": {"line": _fd.get("line"), "over": _fd.get("over"), "under": _fd.get("under")},
    }
print(f"  {len(lines_by_name)} pitchers with lines")

# ── Main loop ─────────────────────────────────────────────────────────────────
THRESHOLD = -130
results = []

for game in SLATE:
    for side in ("away", "home"):
        sp = game[f"{side}_sp"]
        name = sp["name"]
        team = sp["team"]
        hand = sp["hand"]
        opp_lu = game[f"{'home' if side == 'away' else 'away'}_lu"]
        opp_R, opp_L, opp_S = opp_lu["R"], opp_lu["L"], opp_lu["S"]
        lineup_conf = "Yes" if opp_lu.get("confirmed") else "Projected"

        row = {
            "name": name, "team": team, "hand": hand,
            "matchup": game["matchup"], "time": game["time"], "status": game["status"],
            "park": game["park_team"],
            "park_factor": park_overall(game["park_team"]),
            "Opp Lineup": f"{opp_R}R/{opp_L}L/{opp_S}S",
            "Lineup Confirmed": lineup_conf,
            "Weather": game["weather"],
            "PPD%": game.get("ppd_pct", 0),
            "Indoor": game.get("indoor", False),
            "Source": "Odds API",
        }

        fgrow = fg_lookup(fg, name)
        if not fgrow:
            row.update({k: None for k in ["GS","IP","K/9","K%","SwStr%","CSW%","BB%",
                "ERA","FIP","xFIP","SIERA","K-BB%","Whiff%","Hard-Hit%","xERA"]})
            row["Classification"] = "Mixed"
            row["Class Reason"] = "Default (not in FG)"
            row["Multiplier"] = MULTIPLIERS["Mixed"]
            row["Raw Ks"] = None; row["Proj Ks"] = None
            row["Proj Ks v4.0"] = None
            row["Delta v4-v35"] = None
            row["Rule 5"] = "Fail"
            row["Rule 5 Reason"] = "Not in FG dashboard cohort"
            row["BF Call"] = "Watch List"
            row["BF Reason"] = row["Rule 5 Reason"]
            row["Injury Risk"] = "N/A"; row["Risk Flag"] = ""
            row["FG Source"] = "missing"
        else:
            src_label = fgrow.get("_source") or "dashboard"
            row["FG Source"] = src_label
            row["GS Estimated"] = bool(fgrow.get("_gs_estimated"))
            for col in ["GS","IP","K/9","K%","SwStr%","CSW%","BB%","ERA","FIP","xFIP",
                        "SIERA","K-BB%"]:
                row[col] = fgrow.get(col)
            sav = savant_lookup(name)
            row["Whiff%"] = sav.get("whiff") or fgrow.get("Whiff%")
            row["Hard-Hit%"] = sav.get("hard_hit") or fgrow.get("Hard-Hit%")
            row["xERA"] = sav.get("xera") or fgrow.get("xERA")

            cls, reason = classify(name, row.get("SwStr%"), row.get("Whiff%"))
            row["Classification"] = cls
            row["Class Reason"] = reason
            row["Multiplier"] = MULTIPLIERS[cls]

            # ── v3.5p SHADOW (2026-07-05) ────────────────────────────────────
            # Override-ordering bug fix + SwStr-Dom Rising/Established split.
            # Shadow only — does NOT drive BF gate today.
            try:
                from kprop_v35_core import classify_v35p
                known_v35p = None
                lname = name.lower().strip()
                if lname in KNOWN_CLASSES:
                    known_v35p = KNOWN_CLASSES[lname]
                _v35p = classify_v35p(
                    swstr_pct=row.get("SwStr%"),
                    whiff_pct=row.get("Whiff%"),
                    gs_current=row.get("GS"),
                    known_class=known_v35p,
                )
                row["Class v3.5p"] = _v35p["tag"]
                row["Mult v3.5p"]  = _v35p["mult"]
                row["Class Reason v3.5p"] = _v35p["reason"]
            except Exception as _e:
                row["Class v3.5p"] = None
                row["Mult v3.5p"]  = None
                row["Class Reason v3.5p"] = f"shadow err: {_e}"

            gs = row.get("GS"); ip = row.get("IP"); k9 = row.get("K/9")
            if gs is None or ip is None or k9 is None:
                row["Rule 5"] = "Fail"
                row["Rule 5 Reason"] = "Missing GS/IP/K9"
                row["Raw Ks"] = None; row["Proj Ks"] = None
                row["Proj Ks v4.0"] = None; row["Delta v4-v35"] = None
            elif gs < 10:
                row["Rule 5"] = "Fail"
                est_note = " (GS est)" if row.get("GS Estimated") else ""
                row["Rule 5 Reason"] = f"GS={gs} < 10{est_note}"
                row["Raw Ks"] = None; row["Proj Ks"] = None
                row["Proj Ks v4.0"] = None; row["Delta v4-v35"] = None
            elif ip < 25:
                row["Rule 5"] = "Fail"
                row["Rule 5 Reason"] = f"IP={ip} < 25"
                row["Raw Ks"] = None; row["Proj Ks"] = None
                row["Proj Ks v4.0"] = None; row["Delta v4-v35"] = None
            else:
                row["Rule 5"] = "Pass"
                row["Rule 5 Reason"] = ""
                # v3.5 base: (K/9 × IP/GS / 9) × Multiplier
                whole = int(ip); frac = ip - whole
                true_ip = whole + (frac * 10 / 3) if frac > 0 else whole
                ip_per_gs = true_ip / gs
                raw_v35 = (k9 * ip_per_gs / 9) * row["Multiplier"]
                env = env_adjust(row["park"], opp_lu, hand)
                raw_v35_adj = raw_v35 + env
                row["Raw Ks"] = round(raw_v35_adj, 2)
                row["Proj Ks"] = v33_calibrate(raw_v35_adj)

                # v4.0 SHADOW (flags MATCH backtest):
                v40 = predict_v40(
                    k9=k9,
                    ip_per_start_season=ip_per_gs,
                    gs_season=gs,
                    book_line=None,
                    apply_short_mixture=False,
                    apply_calibration=False,
                )
                # Same downstream env adj + v33_calibrate
                raw_v40 = v40["final_proj"] + env
                row["Proj Ks v4.0"] = v33_calibrate(raw_v40)
                row["Delta v4-v35"] = round(row["Proj Ks v4.0"] - row["Proj Ks"], 2)

            row["Injury Risk"] = "Standard" if row["Rule 5"] == "Pass" else "N/A"
            row["Risk Flag"] = ""

        # Lines
        ln = lines_by_name.get(alias_key(name), {}) or lines_by_name.get(norm_name(name).lower(), {})
        dk = ln.get("DK", {}) or {}
        fd = ln.get("FD", {}) or {}
        row["DK Line"]  = dk.get("line")
        row["DK Over"]  = dk.get("over")
        row["DK Under"] = dk.get("under")
        row["FD Line"]  = fd.get("line")
        row["FD Over"]  = fd.get("over")
        row["FD Under"] = fd.get("under")

        gp, gb = best_under_juice(ln, threshold=THRESHOLD)
        sp_price, sp_book = best_under_for_bettor(ln)
        row["Best Under"] = gp; row["Best Under Book"] = gb
        row["Shop Under"] = sp_price; row["Shop Under Book"] = sp_book

        # ── v3.5p SHADOW — young-arm juice flag (does NOT block BF call today) ──
        try:
            from kprop_v35_core import young_arm_juice_check
            _passes, _reason = young_arm_juice_check(
                tag=row.get("Class v3.5p"),
                gs_current=row.get("GS"),
                best_under_juice=sp_price,   # actual best Under for shop-able read
            )
            row["v3.5p Young Arm Flag"] = "" if _passes else "BLOCK"
            row["v3.5p Young Arm Reason"] = _reason or ""
        except Exception as _e:
            row["v3.5p Young Arm Flag"] = ""
            row["v3.5p Young Arm Reason"] = f"shadow err: {_e}"

        if row["DK Line"] is not None:
            row["Best Line"] = row["DK Line"]
        elif row["FD Line"] is not None:
            row["Best Line"] = row["FD Line"]
        else:
            row["Best Line"] = None

        # Residual on v3.5 (BF gating still v3.5)
        if row.get("Proj Ks") is not None and row["Best Line"] is not None:
            row["Residual"] = round(row["Proj Ks"] - row["Best Line"], 2)
        else:
            row["Residual"] = None
        # Also compute v4.0 residual (for shadow log)
        if row.get("Proj Ks v4.0") is not None and row["Best Line"] is not None:
            row["Residual v4.0"] = round(row["Proj Ks v4.0"] - row["Best Line"], 2)
        else:
            row["Residual v4.0"] = None

        # BF gate (V3.5)
        if row.get("BF Call") == "Watch List":
            pass
        elif row["Rule 5"] != "Pass":
            row["BF Call"] = "Watch List"
            row["BF Reason"] = row["Rule 5 Reason"]
        else:
            res = row["Residual"]
            ppd_raw = game.get("ppd_pct", 0) or 0
            try:
                ppd_pct = float(str(ppd_raw).replace('%','').strip() or 0)
            except Exception:
                ppd_pct = 0
            row["PPD%"] = ppd_pct
            ppd_ok = ppd_pct <= 50
            gate_ok = gp is not None and gp <= THRESHOLD
            residual_ok = res is not None and res <= -0.75
            if residual_ok and gate_ok and ppd_ok:
                row["BF Call"] = "Under"
                row["BF Reason"] = (
                    f"Gate PASS: residual {res:+.2f} ≤ -0.75, "
                    f"qualifying Under {gp} ({gb}), Rule 5 pass, PPD ≤ 50%"
                )
            else:
                row["BF Call"] = "No call"
                reasons = []
                if not residual_ok:
                    reasons.append("no residual" if res is None else f"residual {res:+.2f} > -0.75")
                if not gate_ok:
                    reasons.append("no Under price" if gp is None else f"best Under {gp} ({gb}) > -130")
                if not ppd_ok:
                    reasons.append(f"PPD {ppd_pct}% > 50%")
                row["BF Reason"] = "; ".join(reasons)

        results.append(row)

# Save model results
out_results = str(WORKSPACE / f"model_results_{DATE_STR}_{SLATE_TYPE}.json")
with open(out_results, "w") as f:
    json.dump(results, f, indent=2, default=str)
print(f"\nWrote {out_results}")

# ── Append per-pitcher shadow row to WORKSPACE/shadow_v35_vs_v40.csv ──────────
SHADOW_CSV = str(WORKSPACE / "shadow_v35_vs_v40.csv")
shadow_cols = [
    "date","slate","pitcher","team","matchup","K/9","IP","GS","ip_per_start",
    "k9_bucket","v35_proj","v40_proj","delta","best_line","v35_residual",
    "v40_residual","bf_call","actual_ks","actual_result"
]
new_file = not os.path.exists(SHADOW_CSV)
with open(SHADOW_CSV, "a", newline="") as f:
    w = csv.DictWriter(f, fieldnames=shadow_cols)
    if new_file:
        w.writeheader()
    for r in results:
        k9 = r.get("K/9")
        if r.get("Proj Ks v4.0") is None or r.get("Proj Ks") is None:
            continue  # only log pitchers with both projections
        if k9 is None:
            bucket = "unknown"
        elif k9 < 7:
            bucket = "<7"
        elif k9 < 9:
            bucket = "7-9"
        elif k9 < 11:
            bucket = "9-11"
        else:
            bucket = "11+"
        gs = r.get("GS"); ip = r.get("IP")
        ip_per_start = (ip / gs) if gs and ip else None
        w.writerow({
            "date": DATE_STR,
            "slate": SLATE_TYPE.upper(),
            "pitcher": r["name"],
            "team": r["team"],
            "matchup": r["matchup"],
            "K/9": round(k9, 2) if k9 is not None else "",
            "IP": ip,
            "GS": gs,
            "ip_per_start": round(ip_per_start, 2) if ip_per_start else "",
            "k9_bucket": bucket,
            "v35_proj": r["Proj Ks"],
            "v40_proj": r["Proj Ks v4.0"],
            "delta": r["Delta v4-v35"],
            "best_line": r["Best Line"],
            "v35_residual": r["Residual"],
            "v40_residual": r["Residual v4.0"],
            "bf_call": r["BF Call"],
            "actual_ks": "",
            "actual_result": "",
        })
print(f"Appended {sum(1 for r in results if r.get('Proj Ks v4.0') is not None)} rows to {SHADOW_CSV}")

# ── Summary print ─────────────────────────────────────────────────────────────
print()
print(f"{'Name':<22} {'GS':>4} {'IP':>5} {'K/9':>5} {'v3.5':>5} {'v4.0':>5} {'Δ':>5} {'Line':>5} {'Resid':>6} {'BF':<11}")
print("-" * 100)
for r in results:
    fmt = lambda v: ("" if v is None else f"{float(v):.2f}")
    print(f"{r['name'][:22]:<22} "
          f"{('' if r.get('GS') is None else str(r['GS'])).rjust(4)} "
          f"{('' if r.get('IP') is None else str(r['IP'])).rjust(5)} "
          f"{fmt(r.get('K/9')):>5} "
          f"{fmt(r.get('Proj Ks')):>5} "
          f"{fmt(r.get('Proj Ks v4.0')):>5} "
          f"{fmt(r.get('Delta v4-v35')):>5} "
          f"{('' if r.get('Best Line') is None else str(r['Best Line'])).rjust(5)} "
          f"{fmt(r.get('Residual')):>6} "
          f"{r.get('BF Call','')[:11]:<11}")

bf_picks = [r for r in results if r["BF Call"] == "Under"]
watchlist = [r for r in results if r["BF Call"] == "Watch List"]
print(f"\n=== BF Picks: {len(bf_picks)} ===")
for r in bf_picks:
    print(f"  {r['name']}: Under {r['Best Line']} ({r['Best Under']} {r['Best Under Book']}) | residual {r['Residual']:+.2f}")
print(f"=== Watch List: {len(watchlist)} ===")
print(f"=== No call: {sum(1 for r in results if r['BF Call'] == 'No call')} ===")
