"""Layer Line Movement flags onto the late-slate results by comparing to AM.

Run with: python add_line_movement.py 2026-07-09
(Unchanged logic from add_line_movement_example.py — only paths patched.)
"""
import argparse
import json
import sys
from pathlib import Path
from datetime import datetime
from collections import Counter

from _paths import WORKSPACE

ap = argparse.ArgumentParser()
ap.add_argument("date", nargs="?", default=datetime.now().strftime("%Y-%m-%d"))
args = ap.parse_args()
DATE_STR = args.date

am_path = WORKSPACE / f"model_results_{DATE_STR}_am.json"
late_path = WORKSPACE / f"model_results_{DATE_STR}_late.json"
if not am_path.exists() or not late_path.exists():
    raise SystemExit(f"Need both {am_path.name} and {late_path.name} in {WORKSPACE} to compare.")

am_by_name = {r['name']: r for r in json.load(open(am_path))}
late = json.load(open(late_path))


def steam_flag(am_line, pm_line, am_under, pm_under, bf_call):
    if am_line is None or pm_line is None:
        return 'NEW'
    diff = pm_line - am_line
    if bf_call not in ('Under', 'Over'):
        if abs(diff) < 0.25:
            return 'CONFIRMS'
        return 'NEUTRAL'
    if bf_call == 'Under':
        if diff <= -0.5: return 'STEAM'
        if diff >= 0.5:  return 'WARNS'
    else:
        if diff >= 0.5:  return 'STEAM'
        if diff <= -0.5: return 'WARNS'
    if am_under is not None and pm_under is not None:
        if bf_call == 'Under' and pm_under <= am_under - 6:
            return 'STEAM'
        if bf_call == 'Under' and pm_under >= am_under + 6:
            return 'WARNS'
    return 'CONFIRMS'


for r in late:
    name = r['name']
    am = am_by_name.get(name, {})
    am_dk_line = am.get('DK Line'); am_fd_line = am.get('FD Line')
    am_line = am_dk_line if am_dk_line is not None else am_fd_line
    am_under = am.get('Best Under')
    pm_line = r.get('Best Line')
    pm_under = r.get('Best Under')
    r['AM Line'] = am_line
    r['AM Under'] = am_under
    r['AM DK Line'] = am_dk_line
    r['AM FD Line'] = am_fd_line
    r['Line Movement'] = steam_flag(am_line, pm_line, am_under, pm_under, r.get('BF Call'))

json.dump(late, open(late_path, 'w'), indent=2, default=str)

print('Line Movement:', Counter(r['Line Movement'] for r in late))
print('\nMovement detail (BF-eligible + BF picks):')
for r in late:
    if r.get('BF Call') in ('Under', 'Over', 'No call'):
        print(f"  {r['name']:22} AM={r.get('AM Line')} PM={r.get('Best Line')}  "
              f"AM_und={r.get('AM Under')} PM_und={r.get('Best Under')}  "
              f"-> {r['Line Movement']}  ({r.get('BF Call')})")
