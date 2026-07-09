"""
fangraphs_loader.py — Reads the user's FanGraphs CSV export.

The user drops `fangraphs-leaderboards*.csv` (or a fixed name) into the
workspace from their logged-in FG custom dashboard. The daily cron reads
the most recent matching file instead of trying to scrape the paywalled
leaderboard.

Usage:
    from fangraphs_loader import load_fg_csv
    fg = load_fg_csv()   # picks the newest fangraphs-leaderboards*.csv
    # fg: dict keyed by normalized 'first last' (lowercased, ASCII)
    # each value: dict of all columns from the CSV, with numeric coercion

Caller convention: missing columns return None — render "" / "—" per the
"no fabrication" rule.
"""
from __future__ import annotations

import csv
import glob
import json
import os
import unicodedata
from pathlib import Path
from typing import Any, Dict, Optional

from _paths import WORKSPACE
DEFAULT_PATTERN = "fangraphs-leaderboards*.csv"
SPLITS_PATTERN = "fangraphs-splits*.csv"
MANUAL_SUPPLEMENT = Path(__file__).with_name("manual_gs_ip.json")


def _ascii(s: str) -> str:
    """NFD-normalize and strip diacritics so Sánchez == Sanchez."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def _norm_name(name: str) -> str:
    return _ascii(name).strip().lower()


def _coerce(v: str) -> Any:
    if v == "" or v is None:
        return None
    try:
        if "." in v or "e" in v.lower():
            return float(v)
        return int(v)
    except ValueError:
        return v


def find_latest_csv(pattern: str = DEFAULT_PATTERN) -> Optional[Path]:
    """Return the most recently modified matching CSV, or None."""
    matches = glob.glob(str(WORKSPACE / pattern))
    if not matches:
        return None
    return Path(max(matches, key=os.path.getmtime))


def _merge_splits_csv(out: Dict[str, Dict[str, Any]]) -> None:
    """Layer in the FG splits leaderboard (broader 2026 cohort, IP ≥ 20).

    Behavior:
      • If a pitcher is in the primary dashboard CSV, only fill columns the
        primary CSV left as None (never overwrite real dashboard data).
      • If a pitcher is NOT in the primary CSV, create a new row from splits.
      • Tags the row with _source='splits_supplement' when it was created
        entirely from splits, so downstream code can flag the gap.

    Splits CSV is overall season totals (not handedness-filtered).
    """
    splits_path = find_latest_csv(SPLITS_PATTERN)
    if splits_path is None or not splits_path.exists():
        return
    with splits_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("Name") or ""
            key = _norm_name(name)
            if not key:
                continue
            coerced = {k: _coerce(v) for k, v in row.items()}
            if key in out:
                # Layer onto existing dashboard row — don't overwrite
                for col, val in coerced.items():
                    if out[key].get(col) is None and val is not None:
                        out[key][col] = val
            else:
                # New pitcher from splits cohort only
                coerced["_display_name"] = name
                coerced["_source"] = "splits_supplement"
                out[key] = coerced


def _merge_manual_supplement(out: Dict[str, Dict[str, Any]]) -> None:
    """If a manual_gs_ip.json file exists, fill in missing GS/IP/ERA values.

    Only fills cells that are absent or None — never overwrites real CSV data.
    """
    if not MANUAL_SUPPLEMENT.exists():
        return
    try:
        blob = json.loads(MANUAL_SUPPLEMENT.read_text())
    except (json.JSONDecodeError, OSError):
        return
    pitchers = blob.get("pitchers", {})
    for name, fields in pitchers.items():
        key = _norm_name(name)
        # Create a row if the pitcher isn't in the CSV at all
        row = out.setdefault(key, {"_display_name": name})
        for col, val in fields.items():
            if row.get(col) is None:
                row[col] = val


def load_fg_csv(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """
    Load the FG CSV into a dict keyed by normalized name.

    Uses NameASCII when present so accented names resolve cleanly.
    Auto-merges /home/user/workspace/repo/manual_gs_ip.json as a fallback
    when the CSV lacks GS or IP columns.
    """
    if path is None:
        path = find_latest_csv()

    out: Dict[str, Dict[str, Any]] = {}
    if path is not None and path.exists():
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key_name = row.get("NameASCII") or row.get("Name") or ""
                key = _norm_name(key_name)
                if not key:
                    continue
                coerced = {k: _coerce(v) for k, v in row.items()}
                coerced["_display_name"] = row.get("Name", key_name)
                out[key] = coerced

    _merge_splits_csv(out)
    _merge_manual_supplement(out)
    _estimate_missing_gs(out)
    _normalize_percentages(out)
    return out


# Columns that are stored as percentages — some FG exports use decimals (0.13)
# and some use percentage points (13.0). We standardize to percentage points
# (13.0) so the model's classification thresholds (SwStr% ≥ 11, etc.) work the
# same way regardless of CSV format.
_PCT_COLUMNS = {
    "K%", "BB%", "K-BB%", "SwStr%", "CSW%", "LOB%", "GB%", "HR/FB",
    "BABIP", "Whiff%", "Hard-Hit%", "Barrel%",
}


def _normalize_percentages(out: Dict[str, Dict[str, Any]]) -> None:
    """Rescale any percentage column that's stored as decimal fractions.

    Heuristic: if every non-None value for a percentage column across all
    pitchers is <= 1.5, treat the column as decimal fractions and multiply
    by 100. Otherwise leave as-is.

    BABIP and HR/FB legitimately live in 0.0–1.0 range — but the model
    doesn't gate on raw BABIP/HR/FB thresholds, so even if those get
    rescaled to 28.5 the workbook just displays them differently. To be
    safe, exclude BABIP and HR/FB from rescaling.
    """
    SAFE_DECIMAL_COLUMNS = {"BABIP", "HR/FB", "LOB%", "GB%"}
    pct_cols = _PCT_COLUMNS - SAFE_DECIMAL_COLUMNS
    for col in pct_cols:
        vals = [r.get(col) for r in out.values() if isinstance(r.get(col), (int, float))]
        if not vals:
            continue
        if max(vals) <= 1.5:
            # decimal fraction — rescale
            for r in out.values():
                v = r.get(col)
                if isinstance(v, (int, float)):
                    r[col] = v * 100


def _estimate_missing_gs(out: Dict[str, Dict[str, Any]]) -> None:
    """For pitchers with IP but no GS (typically from splits supplement),
    estimate GS as round(IP / 5.0). League-average starter IP/GS ≈ 5.0 in 2026.

    Tags the row with _gs_estimated=True so downstream display can note it.
    Only fills when GS is None and IP >= 25 (Rule 5 floor). Below 25 IP the
    pitcher fails Rule 5 anyway — no need to estimate.
    """
    for key, row in out.items():
        if row.get("GS") is not None:
            continue
        ip = row.get("IP")
        if ip is None or not isinstance(ip, (int, float)) or ip < 25:
            continue
        # FG IP values are baseball-style decimals: 47.1 = 47 IP + 1/3 inning.
        # Convert to true innings before dividing.
        whole = int(ip)
        frac = ip - whole
        true_ip = whole + (frac * 10 / 3) if frac > 0 else whole
        est_gs = max(1, round(true_ip / 5.0))
        row["GS"] = est_gs
        row["_gs_estimated"] = True


def lookup(fg: Dict[str, Dict[str, Any]], full_name: str) -> Dict[str, Any]:
    """Look up by 'First Last' (case/accent insensitive). Returns {} if missing."""
    return fg.get(_norm_name(full_name), {})


if __name__ == "__main__":
    p = find_latest_csv()
    if not p:
        print("[fangraphs_loader] no FG CSV found")
        raise SystemExit(0)
    fg = load_fg_csv(p)
    print(f"[fangraphs_loader] loaded {len(fg)} pitchers from {p.name}")
    # Show a couple of sample rows
    for k in list(fg.keys())[:3]:
        row = fg[k]
        print(f"  {row.get('_display_name'):25s}  "
              f"K/9={row.get('K/9')}  xERA={row.get('xERA')}  "
              f"GS={row.get('GS')}  IP={row.get('IP')}")
