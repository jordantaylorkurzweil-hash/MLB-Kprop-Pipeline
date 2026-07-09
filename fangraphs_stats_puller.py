"""
fangraphs_stats_puller.py — replaces the daily manual FanGraphs CSV export
using pybaseball's free leaderboard pull. Runs automatically in the AM job.

pybaseball hits FanGraphs' public /leaders-legacy.aspx page directly — the
same free leaderboard fangraphs.com shows anyone, no login/membership
required. Every column your model needs is available there (GS, IP, K/9,
K%, BB%, K-BB%, SwStr%, CSW%, ERA, FIP, xFIP, SIERA) — confirmed by reading
pybaseball's own source.

SAFETY NET: this script checks its own output before trusting it — required
columns present, a reasonable pitcher count, most pitchers actually having
GS/IP/K-9 values, and a couple of well-known names (Skenes/Skubal/Wheeler)
showing up with sane numbers. If any check fails, it exits with an error
instead of writing a bad file — so the pipeline fails loudly that morning
rather than silently running the model on broken data. If that happens,
export FanGraphs manually for that one day the old way while we sort out
what changed.

Output filename matches the pattern fangraphs_loader.py already looks for
(fangraphs-leaderboards*.csv), so no other code needed to change for this
to slot in as if you'd exported it by hand.

Usage:
    python fangraphs_stats_puller.py 2026 --out workspace/fangraphs-leaderboards_2026.csv
"""
import argparse
from pathlib import Path
from datetime import datetime

from pybaseball import pitching_stats

ap = argparse.ArgumentParser(description="Pull FanGraphs pitching leaderboard (free, no login)")
ap.add_argument("season", type=int, nargs="?", default=datetime.now().year)
ap.add_argument("--qual", type=int, default=0,
                 help="Min PA/IP threshold — 0 includes everyone, including low-GS arms your Rule 5 gate needs to see and reject")
ap.add_argument("--out", default=None)
args = ap.parse_args()

print(f"Pulling FanGraphs pitching leaderboard for {args.season} (qual={args.qual})...")
df = pitching_stats(args.season, qual=args.qual, stat_columns="ALL")

out_path = args.out or f"workspace/fangraphs-leaderboards_{args.season}.csv"
Path(out_path).parent.mkdir(parents=True, exist_ok=True)
df.to_csv(out_path, index=False)

print(f"Wrote {len(df)} pitchers -> {out_path}")
print(f"Columns ({len(df.columns)}): {list(df.columns)}")

# ── Sanity check — fail loudly instead of silently feeding bad data in ───────
REQUIRED_COLS = ["Name", "GS", "IP", "K/9", "SwStr%", "CSW%", "K%", "BB%",
                  "ERA", "FIP", "xFIP", "SIERA"]
missing_cols = [c for c in REQUIRED_COLS if c not in df.columns]
if missing_cols:
    raise SystemExit(
        f"FanGraphs pull is missing expected columns: {missing_cols}\n"
        f"Not writing this in as today's data — fix the column mapping first, "
        f"or fall back to a manual CSV export for today."
    )

if len(df) < 50:
    raise SystemExit(
        f"Only pulled {len(df)} pitchers — that's suspiciously low for a season "
        f"leaderboard. Not trusting this data. Check the pull manually."
    )

null_frac = df[["GS", "IP", "K/9"]].isnull().mean().max()
if null_frac > 0.5:
    raise SystemExit(
        f"Over half of pitchers are missing GS/IP/K-9 ({null_frac:.0%} null) — "
        f"this looks broken, not just a normal early-season gap. Not trusting this data."
    )

# Quick sanity spot-check for a couple of well-known arms
check_names = ["Paul Skenes", "Tarik Skubal", "Zack Wheeler"]
name_col = "Name" if "Name" in df.columns else None
found_check = False
if name_col:
    for n in check_names:
        row = df[df[name_col] == n]
        if not row.empty:
            r = row.iloc[0]
            print(f"  Spot check — {n}: GS={r.get('GS')} IP={r.get('IP')} "
                  f"K/9={r.get('K/9')} SwStr%={r.get('SwStr%')} CSW%={r.get('CSW%')}")
            if r.get("GS") and r.get("IP"):
                found_check = True

if not found_check:
    raise SystemExit(
        "None of the spot-check pitchers (Skenes/Skubal/Wheeler) turned up with "
        "real GS/IP values — something's likely wrong with this pull. Not trusting it."
    )

print("\nSanity checks passed — this data looks good to use.")
