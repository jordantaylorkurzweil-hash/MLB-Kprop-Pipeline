"""
build_backtest.py — portable version of build_backtest_csv_example.py.

Grades yesterday's (or any date's) BF picks against actual results.

Changes vs the original:
- Merges AM + LATE model_results JSON directly (no separate merged_*.json
  input needed — that used to be a manual/agent step).
- Actuals come from scripts/mlb_results_puller.py (free MLB Stats API)
  instead of a manually-fetched ESPN results file.
- Paths route through WORKSPACE; date is a CLI arg instead of hardcoded.
- STARTER SWAPS (a pitcher got scratched and someone else started) can't be
  auto-detected from a stats API alone — that's a same-day news event. If
  it happens, add an entry to SWAPS below before running, same as before.
  This is the one piece of the old pipeline that still benefits from a
  human (or a quick Perplexity lookup) noticing the news that day — it's
  rare enough that it's not worth automating.

Grading logic (Result, BF WIN/LOSS/PUSH, notable misses) is UNCHANGED.
"""
import argparse
import csv
import json
import sys
from pathlib import Path
from datetime import datetime

from _paths import WORKSPACE

ap = argparse.ArgumentParser(description="Build backtest CSV grading a date's BF picks")
ap.add_argument("date", nargs="?", default=datetime.now().strftime("%Y-%m-%d"))
ap.add_argument("--results-file", default=None,
                 help="Path to mlb_results_puller.py output (default: WORKSPACE/results_<date>.json)")
_args = ap.parse_args()
DATE_STR = _args.date

# ── Load AM + LATE model results, if present ──────────────────────────────────
merged = []
seen_late_names = set()
for slate_type in ("am", "late"):
    p = WORKSPACE / f"model_results_{DATE_STR}_{slate_type}.json"
    if p.exists():
        rows = json.load(open(p))
        for r in rows:
            r["_slate"] = slate_type
            if slate_type == "late":
                seen_late_names.add(r["name"])
        merged.extend(rows)
if not merged:
    raise SystemExit(f"No model_results_{DATE_STR}_am/late.json found in {WORKSPACE} — run run_model.py first.")

# Prefer LATE row over AM row when both exist for the same pitcher (LATE = fresher)
by_name = {}
for r in merged:
    nm = r["name"]
    if nm not in by_name or r["_slate"] == "late":
        by_name[nm] = r
merged = list(by_name.values())

# ── Load actuals (free MLB Stats API via mlb_results_puller.py) ───────────────
results_path = _args.results_file or str(WORKSPACE / f"results_{DATE_STR}.json")
if not Path(results_path).exists():
    raise SystemExit(
        f"Results file not found: {results_path}\n"
        f"Run: python mlb_results_puller.py {DATE_STR} --out {results_path}"
    )
results = json.load(open(results_path))

TEAM_ABBR = {
    'chicago white sox': 'CWS', 'baltimore orioles': 'BAL', 'pittsburgh pirates': 'PIT',
    'philadelphia phillies': 'PHI', 'texas rangers': 'TEX', 'cleveland guardians': 'CLE',
    'detroit tigers': 'DET', 'new york yankees': 'NYY', 'new york mets': 'NYM',
    'toronto blue jays': 'TOR', 'washington nationals': 'WSH', 'boston red sox': 'BOS',
    'st. louis cardinals': 'STL', 'atlanta braves': 'ATL', 'cincinnati reds': 'CIN',
    'milwaukee brewers': 'MIL', 'tampa bay rays': 'TB', 'kansas city royals': 'KC',
    'san diego padres': 'SD', 'chicago cubs': 'CHC', 'minnesota twins': 'MIN',
    'houston astros': 'HOU', 'miami marlins': 'MIA', 'colorado rockies': 'COL',
    'los angeles dodgers': 'LAD', 'athletics': 'ATH', 'oakland athletics': 'ATH',
    'san francisco giants': 'SF', 'arizona diamondbacks': 'ARI', 'los angeles angels': 'LAA',
    'seattle mariners': 'SEA',
}


def ip_to_outs(ip):
    try:
        whole = int(ip)
        frac = round((ip - whole) * 10)
        return whole * 3 + frac
    except Exception:
        return None


actuals_list = [{
    "name": sp.get("name", "").strip(),
    "team": sp.get("team"),
    "ip": sp.get("ip"),
    "k": sp.get("actual_ks"),
    "bf": None,
    "outs": ip_to_outs(sp.get("ip")) if sp.get("ip") is not None else None,
    "final_score": sp.get("final_status", ""),
    "matchup": sp.get("matchup", ""),
} for sp in results]


def find_actual(pitcher_name, team=None):
    parts = pitcher_name.split()
    if len(parts) < 2:
        return None
    last = parts[-1].lower()
    first_initial = parts[0][0].lower()
    candidates = [a for a in actuals_list if a["name"].split()[-1].lower() == last]
    if not candidates:
        return None
    if team:
        tm_match = [a for a in candidates if a.get("team") == team]
        if tm_match:
            candidates = tm_match
    if len(candidates) == 1:
        aparts = candidates[0]["name"].split()
        if len(aparts) >= 2:
            afirst = aparts[0].rstrip(".").lower()
            if afirst == first_initial or afirst == parts[0].lower():
                return candidates[0]
            if team and candidates[0].get("team") == team:
                global_last = sum(1 for a in actuals_list if a["name"].split()[-1].lower() == last)
                if global_last == 1:
                    return candidates[0]
            return None
        return candidates[0]
    for a in candidates:
        aparts = a["name"].split()
        if len(aparts) >= 2:
            afirst = aparts[0].rstrip(".").lower()
            if afirst == first_initial or afirst == parts[0].lower():
                return a
    return None


HEADERS = ['date', 'pitcher', 'team', 'matchup', 'time', 'slate', 'park', 'classification',
           'multiplier', 'GS', 'IP', 'K/9', 'SwStr%', 'CSW%', 'Whiff%', 'K%', 'BB%', 'Proj Ks',
           'DK Line', 'DK Over', 'DK Under', 'FD Line', 'FD Over', 'FD Under', 'Best Line',
           'Best Under', 'Best Under Book', 'Residual', 'BF Call', 'BF Reason',
           'Lineup Confirmed', 'PPD%', 'Rule 5', 'Actual Ks', 'Actual IP', 'Actual BF',
           'Outs Recorded', 'Final Score', 'Result', 'Notes']

# ── Starter swaps (scratches) — fill in manually if one happened this date ────
# Same-day scratches aren't visible in a stats API until after the fact, so
# this is the one spot that still wants a human glance at the news.
# Format: {(team, original_last_name_lower): (replacement_full_name, swap_key)}
SWAPS = {}
swap_actuals = {}
for (team, orig_last), (new_name, swap_key) in SWAPS.items():
    new_last = new_name.split()[-1].lower()
    for a in actuals_list:
        if a.get("team") == team and a["name"].split()[-1].lower() == new_last:
            swap_actuals[swap_key] = a
            break

rows = []
for r in merged:
    name = r["name"]
    last_lower = name.split()[-1].lower()
    team_key = (r.get("team", ""), last_lower)
    actual = None if team_key in SWAPS else find_actual(name, team=r.get("team"))

    best_line = r.get("Best Line")
    result = notes = ""
    actual_k = actual_ip = actual_bf = outs = final_score = ""

    swap_info = SWAPS.get(team_key)
    if swap_info is not None and not actual:
        result = "DNS"
        notes = f'Scratched — {swap_info[0]} started for {r.get("team", "")}'
        actual_k = actual_ip = actual_bf = outs = 0
    elif actual:
        actual_k = actual["k"]
        actual_ip = actual["ip"]
        actual_bf = actual["bf"]
        outs = actual["outs"]
        final_score = actual["final_score"]
        if best_line is not None and actual_k is not None:
            if actual_k > best_line:
                result = "OVER"
            elif actual_k < best_line:
                result = "UNDER"
            else:
                result = "PUSH"
    else:
        notes = "no box score match"

    bf_call = r.get("BF Call", "")
    if bf_call in ("Under", "Over") and result in ("OVER", "UNDER", "PUSH"):
        if result == "PUSH":
            notes = ("BF PUSH; " + notes).rstrip("; ")
        elif (bf_call == "Under" and result == "UNDER") or (bf_call == "Over" and result == "OVER"):
            notes = ("BF WIN; " + notes).rstrip("; ")
        else:
            notes = ("BF LOSS; " + notes).rstrip("; ")

    rows.append({
        "date": DATE_STR, "pitcher": name, "team": r.get("team", ""),
        "matchup": r.get("matchup", ""), "time": r.get("time", ""),
        "slate": "late" if name in seen_late_names else "main",
        "park": r.get("park", ""), "classification": r.get("Classification", ""),
        "multiplier": r.get("Multiplier", ""), "GS": r.get("GS", ""), "IP": r.get("IP", ""),
        "K/9": r.get("K/9", ""), "SwStr%": r.get("SwStr%", ""), "CSW%": r.get("CSW%", ""),
        "Whiff%": r.get("Whiff%", ""), "K%": r.get("K%", ""), "BB%": r.get("BB%", ""),
        "Proj Ks": r.get("Proj Ks", ""), "DK Line": r.get("DK Line", ""),
        "DK Over": r.get("DK Over", ""), "DK Under": r.get("DK Under", ""),
        "FD Line": r.get("FD Line", ""), "FD Over": r.get("FD Over", ""),
        "FD Under": r.get("FD Under", ""), "Best Line": r.get("Best Line", ""),
        "Best Under": r.get("Best Under", ""), "Best Under Book": r.get("Best Under Book", ""),
        "Residual": r.get("Residual", ""), "BF Call": bf_call, "BF Reason": r.get("BF Reason", ""),
        "Lineup Confirmed": r.get("Lineup Confirmed", ""), "PPD%": r.get("PPD%", ""),
        "Rule 5": r.get("Rule 5", ""), "Actual Ks": actual_k, "Actual IP": actual_ip,
        "Actual BF": actual_bf, "Outs Recorded": outs, "Final Score": final_score,
        "Result": result, "Notes": notes,
    })

for swap_key, actual in swap_actuals.items():
    rows.append({h: "" for h in HEADERS} | {
        "date": DATE_STR, "pitcher": actual["name"], "team": actual.get("team", ""),
        "matchup": actual.get("matchup", ""), "slate": "main",
        "Actual Ks": actual["k"], "Actual IP": actual["ip"], "Actual BF": actual["bf"],
        "Outs Recorded": actual["outs"], "Final Score": actual["final_score"],
        "Notes": f"Replacement starter (see SWAPS)",
    })

out_path = str(WORKSPACE / f"backtest_{DATE_STR}_partial.csv")
with open(out_path, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=HEADERS)
    w.writeheader()
    for row in rows:
        w.writerow(row)
print(f"Wrote {len(rows)} rows to {out_path}")

bf_picks = [r for r in rows if r["BF Call"] in ("Under", "Over")]
bf_w = sum(1 for r in bf_picks if "BF WIN" in r["Notes"])
bf_l = sum(1 for r in bf_picks if "BF LOSS" in r["Notes"])
bf_p = sum(1 for r in bf_picks if "BF PUSH" in r["Notes"])
print(f"\nBF Picks: {len(bf_picks)} — {bf_w}W/{bf_l}L/{bf_p}P")
for r in bf_picks:
    print(f"  {r['pitcher']}: {r['BF Call']} {r['Best Line']} | actual {r['Actual Ks']} | {r['Result']} | {r['Notes']}")

print("\nNotable misses (|residual| > 1.0, No call):")
for r in rows:
    try:
        res = float(r["Residual"]) if r["Residual"] != "" else None
    except Exception:
        res = None
    if res is not None and abs(res) > 1.0 and r["BF Call"] == "No call" and r["Result"]:
        print(f"  {r['pitcher']}: residual {res:+.2f}, line {r['Best Line']}, actual {r['Actual Ks']}, {r['Result']}")

wl = sum(1 for r in rows if r["BF Call"] == "Watch List" or r["Rule 5"] == "Watch List")
print(f"\nWatch List: {wl}")
print(f"Total rows: {len(rows)}")
print(f"DNS: {sum(1 for r in rows if r['Result'] == 'DNS')}")
