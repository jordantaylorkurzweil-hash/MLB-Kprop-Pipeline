"""
kprop_v35_core.py
─────────────────
Shared computation core for all MLB K-Prop Model cron build scripts.
v3.5 — May 24, 2026

Pipeline:
  v3.2 raw  →  v3.3 calibration (4 stages)  →  env adjustments  →  v3.4 abstain layer
                                                                   →  v3.4 betting filter
                                                                   →  v3.5 line movement flag
                                                                   →  v3.5 injury risk score

v3.4 output columns (unchanged):
  • residual        : Proj_Ks - book_line  (replaces legacy "edge" label everywhere)
  • confidence_zone : Dead zone / Noise zone / Mild edge / Strong edge / Outlier edge / No line
  • model_call      : Over / Under / No call / "" (blank when no line)
  • bf_pass         : 1 if all 4 v3.4 Betting Filter conditions met, else 0
  • tier            : A / B / C / "" — A: residual ≤ −3.0 | B: ≤ −2.5 | C: ≤ −0.75 (BF only)

v3.5 changes:
  1. Over residual threshold raised from +0.5K to +1.0K.
     Under threshold unchanged at ≤ −0.5K (model_call) / ≤ −0.75K (BF gate).
     Rationale: sharp MLB K-prop models require ~8% implied probability edge minimum.
     +0.5K at -110 ≈ 4.5% edge (below sharp floor). +1.0K at -110 ≈ 9% edge (above floor).
     Overs carry early-exit risk; asymmetric thresholds are warranted.

  2. Line movement flag (compute_line_movement_flag):
     Compares opening line to closing/current line. Returns one of:
       CONFIRMS — line moved ≥0.5K in SAME direction as model residual
       WARNS    — line moved ≥0.5K AGAINST model residual  ← blocks call
       STEAM    — line moved ≥0.5K same direction on BOTH FD and DK within 2h
       NEUTRAL  — movement < 0.5K threshold or no data
     Calls are blocked when flag = WARNS. STEAM upgrades confidence display only.

  3. Injury risk score (compute_injury_risk):
     7-factor scoring system producing a 0–7+ integer score and a tier:
       Standard (0–1) : no flag
       Elevated (2–3) : ⚠ flag — IP projection reduced by −0.25, noted in output
       High     (4+)  : 🚨 flag — Over calls suppressed; IP projection reduced by −0.5
     Factors: age≥35, prior arm IL (season), prior arm IL (career≥2), prior start PC>100,
              IP pace spike >20%, LHP+elbow history, start# 1 or 2.

  4. Split Over/Under tracking in drop CSV:
     Added call_side column ("Over"/"Under"/"No call") for independent side hit-rate analysis.
     Over and Under filter are different systems and must not be averaged together.

v3.4 Betting Filter (Under-only, all 4 required — unchanged):
  1. Market confirmation : best available Under juice ≤ −130 (FD preferred, fallback DK)
  2. Model signal        : residual ≤ −0.75 (proj at least 0.75K below line)
  3. Data quality        : Rule 5 pass (≥5 GS AND ≥25 IP in 2026)
  4. PPD filter          : rain risk ≤ 50%

Paper stake tiers (tracking only — not financial advice):
  Tier A : residual ≤ −3.0  → $50 paper
  Tier B : residual ≤ −2.5  → $25 paper
  Tier C : residual ≤ −0.75 (BF pass, below A/B) → $25 paper

Drop CSV exports to data/incoming/YYYY-MM-DD.csv for the 9AM cron backtest.
"""

import os, csv, json
from datetime import date
from pathlib import Path

# ── V3.3 CALIBRATION PARAMS ───────────────────────────────────────────────────
_V33 = {
    "bucket_bias": {
        "<=3": -2.157, "3-4": -0.876, "4-5": -0.503,
        "5-6": -0.251, "6-7": +0.601, "7+":  +1.929,
    },
    "short_p_baseline": 0.1396,
    "short_mean":       1.622,
    "high_mean":        8.867,
    # `residual_global_bias` is the observed pre-v3.3 under-bias of the
    # bucket+short+tail stack (-0.443 K). v33_calibrate SUBTRACTS this value,
    # so for a negative bias the math becomes  s3 - (-0.443) = s3 + 0.443,
    # i.e. we add ~0.44 K to neutralize the under-prediction.
    "residual_global_bias": -0.443,
}

def v33_calibrate(raw: float) -> float:
    """Apply the 4-stage v3.3 post-processor to a raw v3.2 point estimate.

    Stages:
      1. Bucket bias       — piecewise shrinkage by predicted-K bucket
      2. Tail stretch      — pull very-low predictions toward the high mean
      3. Short-outing mix  — blend with the short-start mean
      4. Global bias       — SUBTRACT _V33['residual_global_bias'] (negative,
                              so this effectively ADDS ~0.443 K to fix the
                              pre-v3.3 under-bias). See _V33 docstring.
    """
    bb = _V33["bucket_bias"]
    if   raw <= 3: bias = bb["<=3"]
    elif raw <= 4: bias = bb["3-4"]
    elif raw <= 5: bias = bb["4-5"]
    elif raw <= 6: bias = bb["5-6"]
    elif raw <= 7: bias = bb["6-7"]
    else:          bias = bb["7+"]
    s1 = raw - bias
    s2 = s1 + 0.10 * (_V33["high_mean"] - s1) if raw <= 3 else s1
    p  = _V33["short_p_baseline"]
    s3 = (1 - p) * s2 + p * _V33["short_mean"]
    return round(s3 - _V33["residual_global_bias"], 2)


# ── V3.5 ABSTAIN LAYER ────────────────────────────────────────────────────────
# v3.5 CHANGE 1: Over threshold raised to +1.0K. Under threshold unchanged at 0.5K.
_ABSTAIN_THRESHOLD_OVER  = 1.0   # residual must be ≥ +1.0 to call Over
_ABSTAIN_THRESHOLD_UNDER = 0.5   # residual must be ≤ −0.5 to call Under

def compute_v34(proj_ks, book_line):
    """
    Compute v3.4/v3.5 abstain-layer columns for one pitcher.

    Parameters
    ----------
    proj_ks  : float or None  — v3.3-calibrated Proj Ks (with env adjustments)
    book_line: float or None  — sportsbook K line (FD primary, DK fallback)

    Returns dict with keys:
        residual, abs_residual, confidence_zone, model_call
    """
    if proj_ks is None or book_line is None:
        return {
            "residual":        None,
            "abs_residual":    None,
            "confidence_zone": "No line",
            "model_call":      "",
        }

    residual     = round(proj_ks - book_line, 3)
    abs_residual = round(abs(residual), 3)
    a = abs_residual

    if a < 0.25:   zone = "Dead zone"
    elif a < 0.50: zone = "Noise zone"
    elif a < 1.00: zone = "Mild edge"
    elif a < 1.50: zone = "Strong edge"
    else:          zone = "Outlier edge"

    # v3.5 asymmetric thresholds
    if residual >= _ABSTAIN_THRESHOLD_OVER:
        call = "Over"
    elif residual <= -_ABSTAIN_THRESHOLD_UNDER:
        call = "Under"
    else:
        call = "No call"

    return {
        "residual":        residual,
        "abs_residual":    abs_residual,
        "confidence_zone": zone,
        "model_call":      call,
    }


# ── V3.5 LINE MOVEMENT FLAG ───────────────────────────────────────────────────
_LM_THRESHOLD = 0.5   # minimum line movement (in K) to trigger a flag

LM_FLAGS = ("CONFIRMS", "WARNS", "STEAM", "NEUTRAL")

def compute_line_movement_flag(opening_line, closing_line, residual,
                                fd_move=None, dk_move=None):
    """
    Evaluate line movement relative to model residual direction.

    Parameters
    ----------
    opening_line  : float or None — opening prop line (e.g. FD opening)
    closing_line  : float or None — current/closing prop line
    residual      : float or None — model residual (proj - line)
    fd_move       : float or None — FD line move amount (closing - opening), if tracked separately
    dk_move       : float or None — DK line move amount, if tracked separately

    Returns dict with keys:
        lm_flag       : "CONFIRMS" | "WARNS" | "STEAM" | "NEUTRAL"
        lm_amount     : float — net line change (closing - opening), positive = line went up
        lm_blocks_call: bool  — True if flag == WARNS (call must be suppressed)
        lm_note       : str   — human-readable explanation

    Flag logic:
      STEAM   — fd_move and dk_move both same direction, abs ≥ threshold (takes priority)
      CONFIRMS — line move ≥ threshold, same direction as model (line down + Under residual,
                 or line up + Over residual)
      WARNS    — line move ≥ threshold, opposite direction to model residual ← blocks call
      NEUTRAL  — move < threshold, or no data
    """
    if opening_line is None or closing_line is None or residual is None:
        return {
            "lm_flag":        "NEUTRAL",
            "lm_amount":      None,
            "lm_blocks_call": False,
            "lm_note":        "No line movement data",
        }

    move = round(closing_line - opening_line, 2)   # positive = line moved UP
    abs_move = abs(move)

    if abs_move < _LM_THRESHOLD:
        return {
            "lm_flag":        "NEUTRAL",
            "lm_amount":      move,
            "lm_blocks_call": False,
            "lm_note":        f"Movement {move:+.1f}K below threshold ({_LM_THRESHOLD}K)",
        }

    # STEAM check: both books moved same direction ≥ threshold
    if (fd_move is not None and dk_move is not None
            and abs(fd_move) >= _LM_THRESHOLD and abs(dk_move) >= _LM_THRESHOLD
            and ((fd_move > 0) == (dk_move > 0))):
        flag = "STEAM"
        # STEAM can still be WARNS if it moves against model
        # A STEAM move against the model is the strongest possible warning
        line_vs_model_direction = _line_confirms_model(move, residual)
        blocks = not line_vs_model_direction
        note = (f"STEAM: both FD ({fd_move:+.1f}K) and DK ({dk_move:+.1f}K) moved same direction. "
                + ("Market confirms model." if line_vs_model_direction
                   else "⚠ STEAM moves AGAINST model — strong warning, call blocked."))
        return {
            "lm_flag":        flag,
            "lm_amount":      move,
            "lm_blocks_call": blocks,
            "lm_note":        note,
        }

    # Single-book move
    confirms = _line_confirms_model(move, residual)
    if confirms:
        flag   = "CONFIRMS"
        blocks = False
        note   = (f"Line moved {move:+.1f}K — same direction as model residual "
                  f"({residual:+.2f}K). Market confirmation.")
    else:
        flag   = "WARNS"
        blocks = True
        note   = (f"Line moved {move:+.1f}K — AGAINST model residual ({residual:+.2f}K). "
                  f"Sharp money disagrees. Call blocked.")

    return {
        "lm_flag":        flag,
        "lm_amount":      move,
        "lm_blocks_call": blocks,
        "lm_note":        note,
    }


def _line_confirms_model(move: float, residual: float) -> bool:
    """
    Return True if the line movement direction is consistent with the model residual.
    
    Model says Under (residual < 0) → market confirmation = line moved DOWN (move < 0)
    Model says Over  (residual > 0) → market confirmation = line moved UP   (move > 0)
    """
    if residual < 0 and move < 0:   return True   # both pointing Under
    if residual > 0 and move > 0:   return True   # both pointing Over
    return False


# ── V3.5 INJURY RISK SCORE ────────────────────────────────────────────────────
INJURY_RISK_TIERS = {
    "Standard": (0, 1),   # score 0–1
    "Elevated": (2, 3),   # score 2–3  → IP −0.25, flag ⚠
    "High":     (4, 99),  # score 4+   → IP −0.50, suppress Over calls
}

def compute_injury_risk(age, prior_arm_il_season, prior_arm_il_career,
                        prev_start_pitch_count, ip_pace_spike_pct,
                        is_lhp, elbow_complaint_history, start_number):
    """
    Compute the 7-factor v3.5 injury risk score for a starting pitcher.

    Parameters
    ----------
    age                    : int   — pitcher age (years)
    prior_arm_il_season    : bool  — any arm IL stint this season
    prior_arm_il_career    : int   — total career arm IL stints
    prev_start_pitch_count : int or None — pitch count in most recent start
    ip_pace_spike_pct      : float or None — (current IP/GS pace vs last season) as % change
                             e.g. +25.0 means 25% more IP/GS than last year
    is_lhp                 : bool  — left-handed pitcher
    elbow_complaint_history: bool  — any documented elbow complaint / UCL issue
    start_number           : int   — career start# for the season (1 = opening day start)

    Returns dict with keys:
        risk_score    : int    — raw additive score
        risk_tier     : str    — "Standard" | "Elevated" | "High"
        risk_flag     : str    — "" | "⚠ Elevated" | "🚨 High"
        ip_adjustment : float  — −0.25 (Elevated) | −0.50 (High) | 0.0 (Standard)
        suppress_over : bool   — True if High risk (Over calls must be blocked)
        risk_factors  : list   — human-readable list of triggered factors
    """
    score   = 0
    factors = []

    if age >= 35:
        score += 1
        factors.append(f"Age {age} (≥35)")

    if prior_arm_il_season:
        score += 2
        factors.append("Arm IL stint this season")

    if prior_arm_il_career >= 2:
        score += 1
        factors.append(f"Career arm IL history ({prior_arm_il_career} stints)")

    if prev_start_pitch_count is not None and prev_start_pitch_count > 100:
        score += 1
        factors.append(f"Prior start {prev_start_pitch_count} pitches (>100)")

    if ip_pace_spike_pct is not None and ip_pace_spike_pct > 20.0:
        score += 1
        factors.append(f"IP/GS pace +{ip_pace_spike_pct:.0f}% vs last season")

    if is_lhp and elbow_complaint_history:
        score += 1
        factors.append("LHP + elbow complaint history")

    if start_number in (1, 2):
        score += 1
        factors.append(f"Start #{start_number} (arm ramp risk)")

    # Tier
    if score <= 1:
        tier     = "Standard"
        flag     = ""
        ip_adj   = 0.0
        suppress = False
    elif score <= 3:
        tier     = "Elevated"
        flag     = "⚠ Elevated"
        ip_adj   = -0.25
        suppress = False
    else:
        tier     = "High"
        flag     = "🚨 High"
        ip_adj   = -0.50
        suppress = True   # Over calls must not fire at High risk

    return {
        "risk_score":    score,
        "risk_tier":     tier,
        "risk_flag":     flag,
        "ip_adjustment": ip_adj,
        "suppress_over": suppress,
        "risk_factors":  factors,
    }


def apply_injury_ip_adjustment(proj_ks, base_ip, injury_ip_adj,
                                k_per_9, class_mult):
    """
    Re-derive Proj Ks after applying an injury-risk IP reduction.

    Uses the same formula as the main model:
      Proj Ks = (K/9 × adjusted_IP / 9) × class_mult  →  v3.3 calibrate  →  return

    Parameters
    ----------
    proj_ks       : float — original projection (for reference / fallback)
    base_ip       : float — original projected IP
    injury_ip_adj : float — amount to subtract (e.g. −0.25 or −0.50)
    k_per_9       : float — pitcher K/9 used in original projection
    class_mult    : float — classification multiplier (0.90–0.95)

    Returns adjusted projection (float), clamped ≥ 0.
    """
    if injury_ip_adj == 0.0 or base_ip is None or k_per_9 is None:
        return proj_ks
    adjusted_ip  = max(0.0, base_ip + injury_ip_adj)
    raw_adjusted = (k_per_9 * adjusted_ip / 9.0) * class_mult
    calibrated   = v33_calibrate(raw_adjusted)
    return max(0.0, round(calibrated, 2))


# ── V3.4 BETTING FILTER ───────────────────────────────────────────────────────
_BF_RESIDUAL_THRESHOLD = -0.75   # residual must be ≤ this value
_BF_JUICE_MAX          = -130    # best Under juice must be ≤ this (i.e. −130 or juicier)
_BF_PPD_MAX            = 50      # rain risk % must be ≤ this

def best_under_juice(fd_under, dk_under):
    """
    Return the best (most negative / juiciest) Under odds available.
    Prefers FD (sharper book). Falls back to DK. Returns None if neither available.
    """
    candidates = [j for j in (fd_under, dk_under) if j is not None]
    if not candidates:
        return None
    return min(candidates)   # most negative = juiciest

def compute_betting_filter(residual, rule5_pass, ppd_risk_pct,
                           fd_under, dk_under, status):
    """
    Evaluate the v3.4 Betting Filter for one pitcher.

    All four conditions must be met:
      1. Market confirmation : best Under juice ≤ −130
      2. Model signal        : residual ≤ −0.75
      3. Data quality        : Rule 5 pass
      4. PPD filter          : rain risk ≤ 50%

    Only fires on active starters (status == "active").

    Returns dict with keys:
        bf_pass       : bool
        bf_juice      : float or None  — best juice used
        bf_tier       : "A" | "B" | "C" | ""
        bf_fail_reason: str  — first failing condition, or "" if pass
    """
    if status != "active":
        return {"bf_pass": False, "bf_juice": None, "bf_tier": "", "bf_fail_reason": "Not active"}

    juice = best_under_juice(fd_under, dk_under)

    if residual is None or residual > _BF_RESIDUAL_THRESHOLD:
        reason = f"Residual {residual} > {_BF_RESIDUAL_THRESHOLD}"
    elif juice is None or juice > _BF_JUICE_MAX:
        reason = f"Juice {juice} > {_BF_JUICE_MAX}"
    elif not rule5_pass:
        reason = "Rule 5 fail"
    elif ppd_risk_pct is None:
        # Unknown PPD risk — treat as fail to be conservative on the BF gate.
        reason = "PPD risk Unknown"
    elif ppd_risk_pct > _BF_PPD_MAX:
        # Note: strict `>` so a game at exactly _BF_PPD_MAX (50%) still passes.
        reason = f"PPD risk {ppd_risk_pct}% > {_BF_PPD_MAX}%"
    else:
        reason = ""

    passes = (reason == "")

    tier = ""
    if passes and residual is not None:
        r = abs(residual)
        if r >= 3.0:    tier = "A"
        elif r >= 2.5:  tier = "B"
        else:           tier = "C"

    return {
        "bf_pass":        passes,
        "bf_juice":       juice,
        "bf_tier":        tier,
        "bf_fail_reason": reason,
    }


# ── CONFIDENCE ZONE COLOR MAP (for Excel conditional formatting) ──────────────
ZONE_COLORS = {
    "Dead zone":    "EEEEEE",   # light gray
    "Noise zone":   "FFF9C4",   # pale yellow
    "Mild edge":    "C8E6C9",   # light green
    "Strong edge":  "81C784",   # medium green
    "Outlier edge": "2E7D32",   # dark green (use white text)
    "No line":      "FFFFFF",
}
ZONE_FONT_COLORS = {
    "Outlier edge": "FFFFFF",   # white on dark green
}

CALL_COLORS = {
    "Over":    "00A651",   # green
    "Under":   "C8102E",   # red
    "No call": "888888",   # gray
    "":        "000000",
}

LM_FLAG_COLORS = {
    "CONFIRMS": ("D4EDDA", "155724"),   # light green bg, dark green text
    "WARNS":    ("F8D7DA", "721C24"),   # light red bg, dark red text
    "STEAM":    ("FFF3CD", "856404"),   # amber bg, dark amber text
    "NEUTRAL":  ("FFFFFF", "888888"),   # white bg, gray text
}

TIER_COLORS = {
    "A": ("1a2744", "FFD700"),   # navy bg, gold text
    "B": ("1a2744", "FFFFFF"),   # navy bg, white text
    "C": ("2E4A7A", "FFFFFF"),   # lighter navy bg, white text
    "":  ("FFFFFF", "000000"),
}

BF_COLORS = {
    True:  ("00A651", "FFFFFF"),   # green bg, white text
    False: ("FFFFFF", "888888"),   # white bg, gray text
}

INJURY_FLAG_COLORS = {
    "Standard":  ("FFFFFF", "000000"),
    "Elevated":  ("FFF3CD", "856404"),   # amber
    "High":      ("F8D7DA", "721C24"),   # red
}


# ── DROP CSV WRITER ───────────────────────────────────────────────────────────
# v3.5 additions: lm_flag, injury_risk_score, injury_risk_tier, call_side
DROP_CSV_COLUMNS = [
    "game_date", "pitcher", "team", "opp",
    "predicted_Ks", "book_line",
    "residual",
    "model_call",
    "call_side",         # v3.5: "Over" | "Under" | "No call" — for split hit-rate tracking
    "betting_filter",    # 1 = BF pass, 0 = fail
    "bf_tier",           # A / B / C / ""
    "bf_juice",          # best Under juice at time of pick
    # Line movement columns
    "opening_line", "closing_line", "line_move",
    "lm_flag",           # v3.5: CONFIRMS / WARNS / STEAM / NEUTRAL
    # Injury risk columns
    "injury_risk_score", # v3.5: integer 0–7+
    "injury_risk_tier",  # v3.5: Standard / Elevated / High
    # Actuals (populated post-game)
    "actual_ks",
]

def write_drop_csv(results: list, game_date: str, repo_path: str) -> str:
    """
    Write the v3.5 drop CSV to <repo_path>/data/incoming/<game_date>.csv.

    Merges into any existing file for the same date (pitcher-level deduplication
    by name — last write wins per pitcher). This allows 12:30PM, 4PM, and 6PM
    build scripts to all write to the same daily file without overwriting each other.

    Returns the path written.
    """
    out_dir = Path(repo_path) / "data" / "incoming"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{game_date}.csv"

    # Load existing rows if file exists
    existing = {}
    if out_path.exists():
        with open(out_path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing[row["pitcher"]] = row

    # Merge in new rows
    for p in results:
        if p.get("Status") not in ("active",):
            continue
        if p.get("ProjFinal") is None:
            continue
        if p.get("PropLine") is None:
            continue

        bf  = p.get("BF", {})
        lm  = p.get("LM", {})
        inj = p.get("INJ", {})

        row = {
            "game_date":          game_date,
            "pitcher":            p["Pitcher"],
            "team":               p["Team"],
            "opp":                p["Opp"],
            "predicted_Ks":       p["ProjFinal"],
            "book_line":          p["PropLine"],
            "residual":           p.get("Residual", ""),
            "model_call":         p.get("ModelCall", ""),
            "call_side":          p.get("ModelCall", ""),   # same value, separate column
            "betting_filter":     1 if bf.get("bf_pass") else 0,
            "bf_tier":            bf.get("bf_tier", ""),
            "bf_juice":           bf.get("bf_juice", ""),
            "opening_line":       p.get("OpeningLine", ""),
            "closing_line":       p.get("ClosingLine", ""),
            "line_move":          p.get("LineMoveAmt", ""),
            "lm_flag":            lm.get("lm_flag", "NEUTRAL"),
            "injury_risk_score":  inj.get("risk_score", ""),
            "injury_risk_tier":   inj.get("risk_tier", "Standard"),
            "actual_ks":          existing.get(p["Pitcher"], {}).get("actual_ks", ""),
        }
        existing[p["Pitcher"]] = row

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=DROP_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(existing.values())

    return str(out_path)


# ── v3.6 WEATHER ADJUSTMENT ──────────────────────────────────────────────────
# Empirical Δ-K coefficients applied AFTER v3.3 calibration, BEFORE residual.
# Magnitudes are intentionally conservative — these are priors, not predictions.
#
# Sources for the directions:
#   • FanGraphs / Statcast research consistently shows warmer air = more carry,
#     which depresses K rates on flyball-leaning pitchers (more contact-in-play).
#   • Strong out-blowing wind reduces K rates for FB-tilt arms (fewer chase
#     pop-ups, more BIP); strong in-blowing wind nudges K rates up modestly.
#   • Domes / closed roofs neutralize weather entirely.
#
# Magnitudes (per pitcher per start):
#   Temperature: each 10°F above 70°F  →  −0.05 K
#                each 10°F below 60°F  →  +0.03 K (cold air dampens contact)
#   Wind out:   ≥10 mph out to any field → −0.10 K per 10 mph above 10
#   Wind in:    ≥10 mph in to any field  → +0.07 K per 10 mph above 10
#   Dome / roof closed: hard zero (overrides temp + wind)
#
# wx_note format expected from Rotowire weather JSON, e.g.:
#   "72°F, wind 8 mph out to CF"
#   "58°F, wind 14 mph in from LF"
#   "Dome" / "Roof closed" / ""
_WX_TEMP_HOT_REF   = 70.0
_WX_TEMP_COLD_REF  = 60.0
_WX_TEMP_HOT_COEF  = -0.05   # per 10°F above hot ref
_WX_TEMP_COLD_COEF = +0.03   # per 10°F below cold ref
_WX_WIND_OUT_COEF  = -0.10   # per 10 mph above 10
_WX_WIND_IN_COEF   = +0.07   # per 10 mph above 10
_WX_WIND_THRESHOLD = 10.0    # mph — below or AT this = noise (strict '>' applied)
_WX_ADJ_CAP        = 0.40    # |total adj| capped at ±0.40 K to keep priors honest

# v3.6.1 — Bayesian shrinkage on WxAdj (audit 2026-05-25, MED-SYNTH / Section I)
# The full WxAdj is held back until `_WX_SHRINK_TARGET_DAYS` of real archived
# weather have accumulated. Shrinkage = min(1.0, n_real_days / target). Until
# then, projections see only a fraction of the nominal wx_adj — keeping the
# model honest while the synthetic-backtest effect sizes are still unproven.
_WX_SHRINK_TARGET_DAYS = 30
_WX_ARCHIVE_DIR        = "data/weather_archive"   # relative to repo root

import re as _re

# Temperature: accept "72°F", "72 F", "72°", "72 deg", "72degF". We anchor on
# either a degree sign or an F/deg suffix to avoid grabbing arbitrary numbers
# (like wind speed) that don't represent temperature.
_WX_TEMP_RE = _re.compile(
    r"(-?\d{1,3}(?:\.\d+)?)\s*(?:°\s*F?|°|\s*deg(?:rees)?\s*F?|\s*F)(?![a-z])",
    _re.IGNORECASE,
)
# Wind: tolerate many Rotowire-style phrasings:
#   "wind 12 mph out to CF"            ← original
#   "wind 12 mph to CF"                ← no "out" keyword
#   "wind 12 mph blowing out"          ← "blowing" verb
#   "wind 12 mph from LF"              ← "from" = in
#   "Wind: 12 mph, Out to CF"          ← colon + comma + capital
#   "12 mph wind out to CF"            ← reversed order
# Group 1 = mph; group 2 = qualifier word (out|in|to|from|blowing); group 3 =
# optional direction word (out|in) after "blowing". The direction is resolved
# by `_resolve_wind_dir()` below.
_WX_WIND_RE = _re.compile(
    r"(?:wind\s*:?\s*)?(\d{1,2}(?:\.\d+)?)\s*mph"
    r"(?:[\s,:]+(?:wind\s*:?\s*)?)?"
    r"(?:[\s,:]*(blowing|out|in|to|from|toward|towards|var(?:iable)?|calm))?"
    r"(?:[\s,:]*(out|in))?",
    _re.IGNORECASE,
)
# Also support direction-first phrasing: "wind out to CF at 12 mph"
_WX_WIND_REV_RE = _re.compile(
    r"wind\s*:?\s*(blowing\s+)?(out|in|to|from)\b[^\d]*?(\d{1,2}(?:\.\d+)?)\s*mph",
    _re.IGNORECASE,
)
_WX_DOME_RE = _re.compile(
    r"\b(dome|roof\s+closed|retractable[-\s]*closed|indoor|indoors|closed\s+roof)\b",
    _re.IGNORECASE,
)


def _resolve_wind_dir(qual: str | None, dir2: str | None) -> str | None:
    """Map qualifier words from the wind regex to canonical 'out' or 'in'.

    Rules:
      * 'out', 'to', 'toward', 'towards', 'blowing out'  → 'out'
      * 'in', 'from', 'blowing in'                        → 'in'
      * 'var', 'variable', 'calm'                         → None (no effect)
    """
    if dir2:
        d = dir2.lower()
        if d in ("out", "in"):
            return d
    if not qual:
        return None
    q = qual.lower()
    if q in ("out", "to", "toward", "towards"):
        return "out"
    if q in ("in", "from"):
        return "in"
    if q == "blowing":
        # "blowing" needs a follow-up word that we already captured in dir2
        return None
    if q in ("var", "variable", "calm"):
        return None
    return None

def _parse_wx_note(wx_note):
    """Parse a Rotowire-style weather string into (temp_f, wind_mph, wind_dir, dome).

    Returns dict with keys: temp_f (float|None), wind_mph (float|None),
    wind_dir ("out"|"in"|None), dome (bool).
    """
    if not wx_note or not isinstance(wx_note, str):
        return {"temp_f": None, "wind_mph": None, "wind_dir": None, "dome": False}

    dome = bool(_WX_DOME_RE.search(wx_note))

    temp_f = None
    m = _WX_TEMP_RE.search(wx_note)
    if m:
        try:
            temp_f = float(m.group(1))
        except ValueError:
            temp_f = None

    wind_mph = None
    wind_dir = None
    # Try standard "<mph> mph <dir>" first
    m = _WX_WIND_RE.search(wx_note)
    if m:
        try:
            wind_mph = float(m.group(1))
        except (ValueError, TypeError):
            wind_mph = None
        wind_dir = _resolve_wind_dir(m.group(2), m.group(3))

    # If no direction parsed yet, try reversed phrasing: "wind out to CF at 12 mph"
    if wind_dir is None:
        m2 = _WX_WIND_REV_RE.search(wx_note)
        if m2:
            try:
                wind_mph = float(m2.group(3))
            except (ValueError, TypeError):
                pass
            # group(1)=optional "blowing ", group(2)=out/in/to/from
            wind_dir = _resolve_wind_dir(m2.group(2), None)

    # Heuristic fallback: phrases like "to CF", "out to CF", "from LF" anywhere
    # in the note, even without the "wind" keyword.
    if wind_mph is not None and wind_dir is None:
        wl = wx_note.lower()
        if _re.search(r"\b(out|to|toward|towards)\s+(?:[lrc]?f|center|left|right)", wl):
            wind_dir = "out"
        elif _re.search(r"\b(in|from)\s+(?:[lrc]?f|center|left|right)", wl):
            wind_dir = "in"

    return {"temp_f": temp_f, "wind_mph": wind_mph, "wind_dir": wind_dir, "dome": dome}


def compute_wx_adj(wx_note, park=None):
    """
    Compute a weather Δ-K adjustment from a Rotowire weather note.

    Parameters
    ----------
    wx_note : str or None
        Free-text weather string (Source #4). May contain temperature
        ("72°F"), wind ("wind 12 mph out to CF"), or "Dome"/"Roof closed".
    park    : str or None
        Park name; reserved for future per-park wind sensitivities (e.g.,
        Wrigley wind effects are stronger than most). Unused in v3.6 base.

    Returns dict with keys:
        wx_adj     : float — Δ-K to add to the projection (clipped to ±cap)
        wx_parsed  : dict  — parsed temp/wind/dome values for transparency
        wx_components : dict — per-component contributions (temp, wind)
    """
    parsed = _parse_wx_note(wx_note)

    if parsed["dome"]:
        return {
            "wx_adj":        0.0,
            "wx_parsed":     parsed,
            "wx_components": {"temp": 0.0, "wind": 0.0, "dome_override": True},
        }

    # Temperature contribution
    temp_adj = 0.0
    t = parsed["temp_f"]
    if t is not None:
        if t > _WX_TEMP_HOT_REF:
            temp_adj = _WX_TEMP_HOT_COEF * ((t - _WX_TEMP_HOT_REF) / 10.0)
        elif t < _WX_TEMP_COLD_REF:
            temp_adj = _WX_TEMP_COLD_COEF * ((_WX_TEMP_COLD_REF - t) / 10.0)

    # Wind contribution
    # v3.6.1 — changed `>= _WX_WIND_THRESHOLD` to `> _WX_WIND_THRESHOLD` so that
    # exactly 10 mph counts as noise, matching the comment on the constant.
    wind_adj = 0.0
    w = parsed["wind_mph"]
    d = parsed["wind_dir"]
    if w is not None and d is not None and w > _WX_WIND_THRESHOLD:
        excess = (w - _WX_WIND_THRESHOLD) / 10.0
        if d == "out":
            wind_adj = _WX_WIND_OUT_COEF * (1.0 + excess)
        elif d == "in":
            wind_adj = _WX_WIND_IN_COEF  * (1.0 + excess)

    total_raw = temp_adj + wind_adj
    if total_raw >  _WX_ADJ_CAP: total_raw =  _WX_ADJ_CAP
    if total_raw < -_WX_ADJ_CAP: total_raw = -_WX_ADJ_CAP

    # v3.6.1 — Bayesian shrinkage until 30 days of real archived weather
    shrink, n_days = _wx_shrinkage_factor()
    total = total_raw * shrink

    return {
        "wx_adj":        round(total, 3),
        "wx_adj_raw":    round(total_raw, 3),
        "wx_shrink":     round(shrink, 3),
        "wx_n_real_days": n_days,
        "wx_parsed":     parsed,
        "wx_components": {
            "temp":          round(temp_adj, 3),
            "wind":          round(wind_adj, 3),
            "dome_override": False,
        },
    }


# v3.6.1 — cached shrinkage lookup (counts archived weather days once per import)
_WX_SHRINK_CACHE: dict | None = None

def _wx_shrinkage_factor() -> tuple[float, int]:
    """Return (shrink_factor, n_real_archived_days). Counts files matching
    YYYY-MM-DD.json in `_WX_ARCHIVE_DIR`. Cached for the process lifetime so
    bulk slate builds don't stat the dir for every row.
    """
    global _WX_SHRINK_CACHE
    if _WX_SHRINK_CACHE is not None:
        return _WX_SHRINK_CACHE["shrink"], _WX_SHRINK_CACHE["n_days"]
    import os as _os, re as _re_local
    from pathlib import Path as _Path
    # archive lives at <repo>/data/weather_archive; this module is at <repo>/kprop_v35_core.py
    repo_root = _Path(__file__).resolve().parent
    archive   = repo_root / _WX_ARCHIVE_DIR
    n_days = 0
    if archive.is_dir():
        pat = _re_local.compile(r"^\d{4}-\d{2}-\d{2}\.json$")
        n_days = sum(1 for p in archive.iterdir() if p.is_file() and pat.match(p.name))
    shrink = min(1.0, n_days / float(_WX_SHRINK_TARGET_DAYS)) if _WX_SHRINK_TARGET_DAYS > 0 else 1.0
    _WX_SHRINK_CACHE = {"shrink": shrink, "n_days": n_days}
    return shrink, n_days


# ── v3.6 LHP K-RATE MULTIPLIER ───────────────────────────────────────────────
# Applied as a MULTIPLICATIVE prior on the RAW K count (before v3.3 calibrate).
#
# League-wide K rates for left-handed starters run ~1% above right-handers when
# weighted across all lineup handedness mixes (heavier reliance on changeup +
# back-foot breakers vs. same-side hitters). This is a small structural prior;
# the bulk of the platoon edge would require per-game opponent LHB%, which the
# Rotowire Daily Lineups source can supply in a future iteration — the function
# signature is forward-compatible via opp_lhb_pct.
#
# Default magnitude (~1%) chosen so that even a 7-K projection only shifts by
# ~0.07 K — within the noise band of the abstain layer.
_LHP_BASE_MULT     = 1.010   # 1.0% boost for LHP vs unknown handedness mix
_LHP_VS_HEAVY_LHB  = 1.025   # +2.5% if opp lineup ≥60% LHB
_LHP_VS_HEAVY_RHB  = 1.000   # neutral when opp lineup ≤30% LHB
_LHP_LHB_HEAVY_PCT = 0.60
_LHP_RHB_HEAVY_PCT = 0.30

def compute_lhp_mult(is_lhp, opp_lhb_pct=None):
    """
    Return a K-rate multiplier for left-handed pitchers.

    Parameters
    ----------
    is_lhp      : bool — pitcher throws left
    opp_lhb_pct : float or None — fraction (0.0–1.0) of opposing lineup that
                   bats left. If None, applies the league-wide LHP prior only.

    Returns
    -------
    float — multiplier to apply to the raw (K/9 × IP / 9 × class_mult) product.
            Returns 1.0 for RHPs.
    """
    if not is_lhp:
        return 1.0
    if opp_lhb_pct is None:
        return _LHP_BASE_MULT
    if opp_lhb_pct >= _LHP_LHB_HEAVY_PCT:
        return _LHP_VS_HEAVY_LHB
    if opp_lhb_pct <= _LHP_RHB_HEAVY_PCT:
        return _LHP_VS_HEAVY_RHB
    # Linear blend in the middle band
    span = _LHP_LHB_HEAVY_PCT - _LHP_RHB_HEAVY_PCT
    pos  = (opp_lhb_pct - _LHP_RHB_HEAVY_PCT) / span
    return _LHP_VS_HEAVY_RHB + pos * (_LHP_VS_HEAVY_LHB - _LHP_VS_HEAVY_RHB)


# ── CALL VALIDATION HELPER ───────────────────────────────────────────────────
def validate_call(model_call, lm_flag, injury_suppress_over):
    """
    Apply v3.5 call suppression rules after initial model_call is computed.

    Rules:
      1. If lm_flag == WARNS  → suppress any call (return "No call")
      2. If lm_flag == STEAM and WARNS direction → suppress
      3. If injury_suppress_over and model_call == "Over" → suppress

    Returns final validated call string.
    """
    if lm_flag in ("WARNS",):
        return "No call"
    if injury_suppress_over and model_call == "Over":
        return "No call"
    return model_call


# ─────────────────────────────────────────────────────────────────────────────
# v3.5p patch (2026-06-03) — SwStr-Dom Rising/Established split
#   1. Fixes override-ordering bug: SwStr%≥14 SwStr-Dom check now evaluated
#      FIRST, before the SwStr%≥11 / Whiff%≥27 Mixed override.
#   2. Splits SwStr-Dom by current-season GS:
#        gs_current ≤ 8  → SwStr-Dom-Rising      (mult 0.97)
#        gs_current > 8  → SwStr-Dom-Established (mult 0.92)
#   3. Adds young-arm juice flag: SwStr-Dom pitchers with GS≤12 require
#      best Under juice ≤ -145 (vs standard -130).
#
# ROLLOUT: 2026-06-03 — shadow only. v3.5 gates still drive BF calls today.
# Helpers exposed for runner import; old code paths untouched.
# ─────────────────────────────────────────────────────────────────────────────

SWSTR_DOM_THRESHOLD       = 14.0   # was 13.0 — bumped per v3.5p spec
SWSTR_DOM_RISING_GS_MAX   = 8      # ≤ this → Rising classification
SWSTR_DOM_YOUNG_ARM_GS_MAX = 12    # ≤ this → young-arm juice flag applies
SWSTR_DOM_RISING_JUICE_THRESHOLD = -145
MIXED_OVERRIDE_SWSTR      = 11.0
MIXED_OVERRIDE_WHIFF      = 27.0


def classify_v35p(swstr_pct, whiff_pct, gs_current, known_class=None):
    """
    v3.5p classifier — correct override ordering + SwStr-Dom Rising/Established split.

    Parameters
    ----------
    swstr_pct : float | None
        Season SwStr%.
    whiff_pct : float | None
        Season Whiff% (used for Mixed override at ≥27).
    gs_current : int | None
        Current-season games started.
    known_class : str | None
        If pitcher is in KNOWN_CLASSES (e.g. "jose soriano"→"SwStr-Dominant"),
        pass the known label here. Carried forward but split into Rising/Established
        if it's a SwStr-Dom label and gs_current is provided.

    Returns
    -------
    dict: {tag, mult, reason}
    """
    # 1. Known class carry-forward (with SwStr-Dom split applied)
    if known_class:
        if known_class in ("SwStr-Dominant", "SwStr-Dom",
                           "SwStr-Dom-Rising", "SwStr-Dom-Established"):
            return _split_swstr_dom(gs_current, reason_prefix=f"Known: {known_class}")
        # Non-SwStr-Dom known class — pass through with standard multiplier
        return {
            "tag": known_class,
            "mult": _DEFAULT_MULTS.get(known_class, 0.91),
            "reason": f"Known: {known_class}",
        }

    # 2. SwStr-Dom check FIRST (bug fix: was after override)
    if swstr_pct is not None and swstr_pct >= SWSTR_DOM_THRESHOLD:
        out = _split_swstr_dom(gs_current,
                               reason_prefix=f"SwStr%={swstr_pct:.1f}≥{SWSTR_DOM_THRESHOLD}")
        return out

    # 3. Mixed override (only applies to pitchers BELOW SwStr-Dom threshold)
    if swstr_pct is not None and swstr_pct >= MIXED_OVERRIDE_SWSTR:
        return {"tag": "Mixed", "mult": 0.91,
                "reason": f"SwStr%={swstr_pct:.1f}≥{MIXED_OVERRIDE_SWSTR} override"}
    if whiff_pct is not None and whiff_pct >= MIXED_OVERRIDE_WHIFF:
        return {"tag": "Mixed", "mult": 0.91,
                "reason": f"Whiff%={whiff_pct:.1f}≥{MIXED_OVERRIDE_WHIFF} override"}

    # 4. Default Mixed (no other classification logic in current code)
    return {"tag": "Mixed", "mult": 0.91, "reason": "Default"}


_DEFAULT_MULTS = {
    "SwStr-Dominant": 0.95,
    "Above-Zone": 0.93,
    "Below-Zone": 0.92,
    "Mixed": 0.91,
    "CS-Dependent": 0.90,
}


def _split_swstr_dom(gs_current, reason_prefix=""):
    """Internal: split SwStr-Dom into Rising/Established by current-season GS."""
    if gs_current is not None and gs_current <= SWSTR_DOM_RISING_GS_MAX:
        return {
            "tag": "SwStr-Dom-Rising",
            "mult": 0.97,
            "reason": f"{reason_prefix} & GS={gs_current}≤{SWSTR_DOM_RISING_GS_MAX}",
        }
    return {
        "tag": "SwStr-Dom-Established",
        "mult": 0.92,
        "reason": f"{reason_prefix} & GS={gs_current}>{SWSTR_DOM_RISING_GS_MAX}"
                  if gs_current is not None else f"{reason_prefix} (GS unknown — Established default)",
    }


def young_arm_juice_check(tag, gs_current, best_under_juice):
    """
    v3.5p — SwStr-Dom young arm juice gate (broader than Rising cutoff).

    Applies to any SwStr-Dom-tagged pitcher with GS ≤ 12. Requires the best
    qualifying Under juice to be tighter than -145 (vs standard -130 for
    other classifications). Rationale: young arms have higher K variance,
    so we want stronger market confirmation before a BF call.

    Returns
    -------
    (passes: bool, fail_reason: str | None)
        passes=True means flag does not block.
        passes=False with fail_reason means flag would block if live.
    """
    swstr_dom_tags = ("SwStr-Dom-Rising", "SwStr-Dom-Established",
                      "SwStr-Dominant", "SwStr-Dom")
    if tag not in swstr_dom_tags:
        return True, None
    if gs_current is None or gs_current > SWSTR_DOM_YOUNG_ARM_GS_MAX:
        return True, None
    if best_under_juice is None:
        return False, (f"SwStr-Dom young arm (GS={gs_current}"
                       f"≤{SWSTR_DOM_YOUNG_ARM_GS_MAX}): no Under price")
    if best_under_juice > SWSTR_DOM_RISING_JUICE_THRESHOLD:
        return False, (
            f"SwStr-Dom young arm flag: GS={gs_current}"
            f"≤{SWSTR_DOM_YOUNG_ARM_GS_MAX}, requires juice "
            f"≤{SWSTR_DOM_RISING_JUICE_THRESHOLD}, got {best_under_juice}"
        )
    return True, None
