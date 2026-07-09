"""
kprop_v40_core.py
─────────────────
MLB K-Prop Model — v4.0 projection core.
Replaces the v3.2 base estimator. v3.3-v3.5 calibration/abstain/filter layers
are preserved and wired to the new base output.

--- What changed from v3.5 ---

v3.2 base (replaced):
  Multiple hand-crafted inputs collapsed into a single shrinkage-prone point
  estimate. Predicted std = 1.39, actual std = 2.39 (ratio 0.57). Model
  explained R2 = 0.049 of actual variance, worse than the naive mean baseline
  on MAE. Bucket bias corrections in v3.3 were compensating for this shrinkage
  rather than modeling structure.

v4.0 base (this file):
  base_proj = K/9 * projected_IP / 9
  where:
    K/9          — season-to-date FanGraphs K/9 (primary rate signal)
    projected_IP — pitcher season avg IP/start, clipped to [IP_MIN, IP_MAX],
                   replaced by league average when GS < GS_MIN_RELIABLE
  Optional book blend (when a line is available):
    final_proj = ALPHA_MODEL * base_proj + ALPHA_BOOK * book_line
    alpha values learned walk-forward from training data (default 0.75/0.25)

Walk-forward OOF backtest results (n=272, weeks 16-22):
  Naive mean MAE : 1.966
  v3.2 MAE       : 1.972  R2=0.061
  v3.3 MAE       : 1.939
  v4.0 base MAE  : 1.691  R2=0.242
  v4.0 +blend MAE: 1.700  R2=0.229   (blend slightly hurts at current alpha)
  Book line MAE  : 1.818  (lined starts only, n=236)
  v4.0 vs book   : 1.684  (v4.0 beats book on lined subset, n=236)
  Improvement vs v3.2: +0.272 K  p<0.0001 (paired t-test)

  REPRODUCIBILITY (added 2026-05-30):
  These numbers were computed against a point-in-time FanGraphs SP leaderboard
  snapshot. That snapshot is an external, point-in-time dataset (not model
  output), committed for reproducibility at:
      data/reference/fangraphs_sp_snapshot_2026-05-29.csv
  Pull source: FanGraphs Leaderboards (SP/RP, season-to-date 2026), exported
  2026-05-29. To re-run:  python backtest_v40.py
  CAVEAT: a single season-to-date snapshot introduces mild lookahead for the
  earliest backtest weeks (a pitcher's end-of-window K/9 is used for an early
  start). K/9 is the primary feature; the effect is small but should be
  disclosed. v4.1 should log a weekly point-in-time FG snapshot to close this.

--- What is NOT changed ---
  v3.3 calibration pipeline (4 stages) is preserved but applied to the new base.
  v3.4/v3.5 abstain layer, betting filter, line-movement flag, injury score:
  all unchanged in kprop_v35_core.py. v4.0 only replaces predict_base().

--- Ceilings and known limitations ---
  R2=0.242 means 75.8% of per-start variance is irreducible at pre-game.
  Primary noise source: start length (IP/start from season avg correlates
  r=-0.021 with actual per-start Ks — meaning the specific game IP is
  essentially unpredictable from pre-game features alone).
  Short outings (actual <=2K, 14.9% of starts) are not distinguishable
  pre-game; their residuals remain large.

--- Roadmap to v4.1 ---
  To make P(short) pitcher-conditional, the backtest schema needs per-start:
    pitch_count_cap   — explicit limit (post-IL, opener, etc.)
    days_rest         — actual rest days
    recent_pc_avg     — rolling 3-start pitch count avg
  These are TODO markers below and in build_v40_model.py.
  Until those columns exist in data/backtest.csv, P(short) = league_avg = 0.149.

--- DEPLOYMENT NOTE (cron templates) ---
  The 12:30/4/6 PM build templates call predict_v40() with:
      apply_short_mixture=False   # league-level mixture hurts DirAcc (64.4%->58.5%)
      apply_calibration=False     # v3.3 buckets re-fit downstream by v33_calibrate
      book_line=None              # blend hurts MAE at current alpha
  i.e. they use the PURE BASE (base_proj == final_proj), then feed final_proj
  into the existing v3.3->v3.5 pipeline. Short-mix/blend remain in this file,
  OFF by default in production, until v4.1 makes P(short) pitcher-conditional.
"""

from __future__ import annotations
import math
from typing import Optional

# ── PROJECTION CONSTANTS ─────────────────────────────────────────────────────

# IP projection clipping bounds (innings per start)
IP_MIN = 4.5    # floor — below this is likely a spot-start / opener signal
IP_MAX = 7.0    # ceiling — cap small-sample IP/start outliers (e.g. Ben Brown's 11 IP/GS at 4 starts)

# Minimum starts before we trust a pitcher's own IP/start over league average
GS_MIN_RELIABLE = 5

# League fallback IP (clipped median from 2026 season-to-date)
LEAGUE_AVG_IP = 5.52   # update each week from FanGraphs SP leaderboard

# Bayesian blend: final = ALPHA_MODEL * base_proj + ALPHA_BOOK * book_line
# Walk-forward optimized on 2026 backtest. Book blend slightly hurts overall MAE
# but helps on confidence-zone calls where model and line agree directionally.
# Set ALPHA_BOOK = 0.0 to disable blend (pure model).
ALPHA_MODEL = 0.75
ALPHA_BOOK  = 0.25

# Short-outing mixture constants (league-level until pitcher-conditional features land)
SHORT_K_THRESHOLD = 2.0    # actual Ks at or below this = "short outing"
SHORT_P_LEAGUE    = 0.149  # empirical rate from 2026 backtest (44/296)
SHORT_MEAN_K      = 1.66   # empirical mean Ks for short outings


# ── V4.0 BASE PROJECTION ─────────────────────────────────────────────────────

def project_ip(ip_per_start_season: Optional[float],
               gs_season: Optional[int]) -> float:
    """
    Return a per-start IP projection for one pitcher.

    Uses the pitcher's own season avg IP/start when GS >= GS_MIN_RELIABLE,
    clipped to [IP_MIN, IP_MAX] to remove opener/small-sample outliers.
    Falls back to LEAGUE_AVG_IP otherwise.

    TODO (v4.1): blend in pitch_count_cap, days_rest, recent_pc_avg
    as additive adjustments once those columns exist in the backtest schema.
    """
    if ip_per_start_season is None or gs_season is None or gs_season < GS_MIN_RELIABLE:
        return LEAGUE_AVG_IP
    return float(max(IP_MIN, min(IP_MAX, ip_per_start_season)))


def project_base(
    k9: float,
    ip_per_start_season: Optional[float] = None,
    gs_season: Optional[int] = None,
    book_line: Optional[float] = None,
    alpha_model: float = ALPHA_MODEL,
    alpha_book: float = ALPHA_BOOK,
) -> dict:
    """
    Compute the v4.0 base projection for one pitcher start.

    Parameters
    ----------
    k9                  : season-to-date K/9 from FanGraphs (primary rate signal)
    ip_per_start_season : season avg IP/start from FanGraphs (optional; falls back
                          to LEAGUE_AVG_IP if GS < GS_MIN_RELIABLE)
    gs_season           : season starts (for reliability threshold)
    book_line           : sportsbook K total (FD preferred, DK fallback, None if unavailable)
    alpha_model         : model weight in Bayesian blend (ignored if no book_line)
    alpha_book          : book weight in Bayesian blend  (ignored if no book_line)

    Returns
    -------
    dict with keys:
        base_proj   : K/9 * projected_IP / 9  (pre-blend)
        projected_ip: IP used in projection
        final_proj  : blended projection (= base_proj if no book_line)
        blend_used  : bool — True if book blend was applied
    """
    projected_ip = project_ip(ip_per_start_season, gs_season)
    base_proj    = k9 * projected_ip / 9.0

    if book_line is not None and alpha_book > 0:
        final_proj = alpha_model * base_proj + alpha_book * float(book_line)
        blend_used = True
    else:
        final_proj = base_proj
        blend_used = False

    return {
        "base_proj":    round(base_proj, 3),
        "projected_ip": round(projected_ip, 2),
        "final_proj":   round(final_proj, 3),
        "blend_used":   blend_used,
    }


# ── SHORT-OUTING MIXTURE (v4.0 league-level; pitcher-conditional in v4.1) ───

def apply_short_outing_mixture(
    proj: float,
    p_short: float = SHORT_P_LEAGUE,
    short_mean: float = SHORT_MEAN_K,
) -> float:
    """
    Blend the point projection with the short-outing mean.

    E[K] = (1 - p_short) * proj + p_short * short_mean

    Currently uses league-average p_short for all pitchers.
    v4.1 will replace p_short with a logistic function of:
        pitch_count_cap, days_since_IL, recent_pc_avg, bullpen_game_flag
    TODO markers for those inputs are below.

    NOTE: At the league-average p_short, this mixture compresses every
    projection toward 1.66K, which lowers directional accuracy in backtest
    (64.4% -> 58.5%). It is therefore OFF by default in production
    (predict_v40(apply_short_mixture=False)). Keep until v4.1 makes p_short
    pitcher-conditional.
    """
    # TODO v4.1: replace p_short with pitcher-conditional logistic
    # p_short = logistic(b0
    #     + b1 * is_pitch_count_capped       # post-IL, explicit cap
    #     + b2 * days_since_il               # 0 if healthy, n days if returning
    #     + b3 * (recent_pc_avg - 85)        # heavy recent workload -> lower
    #     + b4 * is_bullpen_game_flag         # opener / bulk reliever
    # )
    return (1.0 - p_short) * proj + p_short * short_mean


# ── CALIBRATION RESIDUALS (from v3.3, applied to new base) ───────────────────
# These bucket biases were fit on v3.2 predictions. After switching to v4.0 base,
# they should be re-fit via build_v40_model.py once sufficient v4.0 predictions
# accumulate. Until then, applying them to the v4.0 base provides a conservative
# correction.

_V40_CALIBRATION = {
    "bucket_bias": {
        # Fit on v3.2 predictions, to be re-fit on v4.0 predictions.
        # v4.0 base already largely de-biases the tails, so these are small.
        # Re-run build_v40_model.py after 50+ new v4.0 predictions to refresh.
        "<=3": -2.157, "3-4": -0.876, "4-5": -0.503,
        "5-6": -0.251, "6-7": +0.601, "7+": +1.929,
    },
    "global_bias": -0.443,   # pre-v3.3 under-bias: subtracting this ADDS ~0.44K
    "calibration_source": "v3.3 (to be re-fit on v4.0 predictions at n>=50)",
}


def calibrate_v40(raw: float, apply_bucket: bool = False) -> float:
    """
    Apply v3.3-origin bucket calibration to a v4.0 base projection.

    Set apply_bucket=False (default) to skip bucket corrections — recommended
    until the calibration is re-fit on actual v4.0 predictions.
    The global bias correction is always applied.
    """
    if apply_bucket:
        bb = _V40_CALIBRATION["bucket_bias"]
        if   raw <= 3: bias = bb["<=3"]
        elif raw <= 4: bias = bb["3-4"]
        elif raw <= 5: bias = bb["4-5"]
        elif raw <= 6: bias = bb["5-6"]
        elif raw <= 7: bias = bb["6-7"]
        else:          bias = bb["7+"]
        raw = raw - bias

    # Subtracting a negative global_bias effectively ADDS to correct under-prediction.
    return round(raw - _V40_CALIBRATION["global_bias"], 2)


# ── FULL V4.0 PIPELINE ───────────────────────────────────────────────────────

def predict_v40(
    k9: float,
    ip_per_start_season: Optional[float] = None,
    gs_season: Optional[int] = None,
    book_line: Optional[float] = None,
    apply_short_mixture: bool = True,
    apply_calibration: bool = False,   # off until re-fit on v4.0 predictions
) -> dict:
    """
    Full v4.0 projection pipeline for one pitcher start.

    Returns dict with:
        base_proj      : K/9 * projected_IP / 9
        projected_ip   : IP used
        blend_used     : bool
        short_adj      : float — post-mixture projection (if apply_short_mixture)
        final_proj     : final projection (rounds to 2dp)
        pipeline_steps : dict showing each stage value for debugging

    PRODUCTION CALL (cron templates): predict_v40(k9, ip_per_start, gs,
    book_line=None, apply_short_mixture=False, apply_calibration=False) so that
    final_proj == base_proj (pure K/9*IP). The downstream v3.3->v3.5 pipeline
    (v33_calibrate, lineup/park/weather adj, abstain, filter) is then applied
    in the template exactly as before.
    """
    base = project_base(k9, ip_per_start_season, gs_season, book_line)
    working = base["final_proj"]
    steps = {"after_base_blend": round(working, 3)}

    if apply_short_mixture:
        working = apply_short_outing_mixture(working)
        steps["after_short_mixture"] = round(working, 3)

    if apply_calibration:
        working = calibrate_v40(working, apply_bucket=True)
        steps["after_calibration"] = round(working, 3)

    return {
        "base_proj":    base["base_proj"],
        "projected_ip": base["projected_ip"],
        "blend_used":   base["blend_used"],
        "short_adj":    steps.get("after_short_mixture"),
        "final_proj":   round(working, 2),
        "pipeline_steps": steps,
    }


# ── ALPHA OPTIMIZER (call once per training fold) ────────────────────────────

def fit_blend_alpha(
    train_k9: list,
    train_ip: list,
    train_gs: list,
    train_lines: list,
    train_actuals: list,
    alpha_grid: tuple = tuple(x / 100 for x in range(0, 101, 5)),
) -> float:
    """
    Walk-forward helper: find the model-weight alpha that minimizes MAE
    on the training fold. Only uses starts where a book line is available.

    Returns the optimal alpha (0.0 = full book, 1.0 = full model).
    """
    lined = [
        (k, ip, gs, line, act)
        for k, ip, gs, line, act in zip(train_k9, train_ip, train_gs, train_lines, train_actuals)
        if line is not None
    ]
    if len(lined) < 10:
        return ALPHA_MODEL   # not enough data, use default

    best_alpha, best_mae = ALPHA_MODEL, float("inf")
    for alpha in alpha_grid:
        errs = []
        for k, ip, gs, line, act in lined:
            proj = k * max(IP_MIN, min(IP_MAX, ip if gs >= GS_MIN_RELIABLE else LEAGUE_AVG_IP)) / 9.0
            pred = alpha * proj + (1 - alpha) * line
            errs.append(abs(pred - act))
        mae = sum(errs) / len(errs)
        if mae < best_mae:
            best_mae = mae
            best_alpha = alpha
    return best_alpha


# ── QUICK SMOKE TEST ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        # (label, k9, ip/start, GS, book_line, expected_range)
        # Ranges reflect v4.0 pipeline: base -> short-mixture -> optional blend
        ("Elite SP, no line",    14.0, 6.0, 15, None,  (7.0, 10.0)),
        ("Avg SP, with line",     8.5, 5.5, 12,  5.5,  (4.0,  5.5)),
        ("Low-K SP, few GS",      5.0, 5.0,  3, None,  (2.0,  4.0)),
        ("Workhorse, deep line",  9.5, 7.0, 20,  6.5,  (5.5,  8.0)),
    ]

    print("v4.0 smoke test")
    print(f"{'Label':<30} {'base':>6} {'final':>7} {'blend':>6} {'ip':>5}")
    print("-" * 60)
    all_pass = True
    for label, k9, ip, gs, line, (lo, hi) in tests:
        r = predict_v40(k9, ip, gs, line)
        ok = lo <= r["final_proj"] <= hi
        all_pass = all_pass and ok
        flag = "" if ok else "  FAIL"
        print(f"{label:<30} {r['base_proj']:>6.2f} {r['final_proj']:>7.2f} "
              f"{'yes' if r['blend_used'] else 'no':>6} {r['projected_ip']:>5.1f}{flag}")

    # Short-outing mixture check
    raw = 6.5
    mixed = apply_short_outing_mixture(raw)
    assert abs(mixed - ((1 - SHORT_P_LEAGUE) * raw + SHORT_P_LEAGUE * SHORT_MEAN_K)) < 1e-9
    print(f"\nShort mixture: {raw} -> {mixed:.3f}  (p_short={SHORT_P_LEAGUE:.3f})  OK")

    # Blend alpha check
    assert predict_v40(8.0, 6.0, 12, book_line=None)["blend_used"] == False
    assert predict_v40(8.0, 6.0, 12, book_line=5.5)["blend_used"] == True
    print("Blend toggle: OK")

    # Production-call invariant: pure base == final
    prod = predict_v40(11.0, 6.0, 14, book_line=None,
                       apply_short_mixture=False, apply_calibration=False)
    # base_proj is rounded to 3dp, final_proj to 2dp -> allow rounding tolerance
    assert abs(prod["final_proj"] - prod["base_proj"]) < 0.01
    print(f"Production call (pure base): base={prod['base_proj']} final={prod['final_proj']}  OK")

    print(f"\nAll tests {'PASSED' if all_pass else 'FAILED — check range expectations'}")
