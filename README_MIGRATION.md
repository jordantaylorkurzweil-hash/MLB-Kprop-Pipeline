# K-Prop Pipeline — Migration off Perplexity

## What changed

Your v3.5/v3.6.1 model logic is **untouched** — same classification multipliers,
Rule 5 gates, park/platoon adjustment, v3.3 calibration, BF gate thresholds.
All I changed is *how the data gets in and where the code runs*.

| Piece | Before | Now | Cost |
|---|---|---|---|
| FanGraphs stats | Perplexity agent (or you) exports CSV | **Same — still a manual export** | $0, was always $0 |
| Baseball Savant (Whiff%, Hard-Hit%, xERA) | Perplexity agent | `pybaseball`, direct script | $0 |
| Odds (DK/FD strikeout lines) | Perplexity agent | The Odds API, direct script | $0 (free tier) |
| RotoWire slate (lineups/weather/PPD%) | Perplexity agent scraping RotoWire | **MLB Stats API + Open-Meteo**, direct script | $0 |
| Actual results (backtest grading) | Perplexity agent (ESPN) | MLB Stats API, direct script | $0 |
| Running the model + building the Excel workbook | Perplexity compute (paid per run) | GitHub Actions (free tier: 2,000 min/month) | $0 |
| Starter scratches/swaps same-day | Perplexity noticing the news | **Still needs a human glance** — see `runners/build_backtest.py` `SWAPS` dict | manual, rare |

Net effect: three Perplexity agent runs a day → three free GitHub Actions
runs a day, hitting real APIs directly with plain Python instead of paying
an LLM to browse pages and re-derive the same pipeline every time.

## One important fix I made along the way

`odds_api_loader.py` was authenticating via Perplexity's internal
"custom-cred" proxy, which only exists inside Perplexity's sandbox. Outside
it, that call would 401. I patched it to read an `ODDS_API_KEY` environment
variable and pass it as the `apiKey` query param directly — see setup below.

## What you still do manually (and why)

**FanGraphs CSV export.** FanGraphs' full leaderboard is paywalled — this was
never something Perplexity was scraping either, per the loader's own docstring.
Drop `fangraphs-leaderboards*.csv` (and `fangraphs-splits*.csv` if you use it)
into `workspace/` and `git push` it before the 11am run. Takes under a minute,
same as before.

**Starter scratches.** If a pitcher gets scratched same-day and someone else
starts, no free API will flag that for you instantly — add an entry to the
`SWAPS` dict at the top of `runners/build_backtest.py` before the 6am backtest
run. This is rare enough that it's not worth automating.

## What's approximate, not identical

- **PPD% (rain risk)** is now a same-day precipitation-probability proxy from
  Open-Meteo (free, no key), not RotoWire's proprietary number. Directionally
  similar, worth a gut-check against a live line the first few times.
- **Confirmed lineups** only populate once MLB posts them (~1-2 hrs before
  first pitch), same limitation as before — the model already falls back to
  non-lineup-weighted park factors when unconfirmed, unchanged.

## Setup

1. Push this folder to a new GitHub repo (private is fine — free tier still
   gives 2,000 Actions minutes/month on private repos).
2. Get a free Odds API key: https://the-odds-api.com (500 requests/month free;
   this pipeline uses roughly 1-2 calls/game/day).
3. In the repo: **Settings → Secrets and variables → Actions → New repository
   secret** → name it `ODDS_API_KEY`, paste the key.
4. The workflow (`.github/workflows/kprop_pipeline.yml`) is already scheduled
   for 11:00 AM / 5:30 PM / 6:00 AM ET. Cron times are UTC and hardcoded for
   EDT (summer) — shift by an hour each way if you're running this outside
   the daylight-saving window.
5. Each morning before 11am ET: export your FanGraphs CSV, drop it in
   `workspace/`, commit, push.
6. Outputs (workbook, backtest CSV, model results) show up as a downloadable
   artifact on each workflow run under the **Actions** tab.

## Testing it yourself first

I couldn't hit the live MLB Stats API / Odds API / Open-Meteo from my own
sandbox to fully dry-run this end-to-end (network's locked down to package
registries only on my end) — I verified everything compiles, imports cleanly,
and matches the documented API shapes, but the very first live run is worth
watching closely. Trigger it manually via **Actions → MLB K-Prop Pipeline →
Run workflow** and check the logs before trusting the 11am auto-run. If
something's off (a field name, a schema mismatch), paste me the error and
I'll patch it.

## File map

```
repo/                    # Model logic — unchanged from your bundle
  kprop_v35_core.py       #   classification, multipliers, calibration
  kprop_v40_core.py       #   v4.0 shadow model
  odds_api_loader.py      #   PATCHED: real apiKey auth instead of proxy
  fangraphs_loader.py     #   PATCHED: portable WORKSPACE path
  park_factors_loader.py  #   unchanged
  savant_pull.py          #   unchanged — already used pybaseball, already free
  _paths.py               #   NEW: shared workspace path resolver

scripts/
  mlb_slate_puller.py     # NEW: replaces RotoWire scrape (MLB Stats API + Open-Meteo)
  mlb_results_puller.py   # NEW: replaces manual results fetch (MLB Stats API)
  park_coords.py          # NEW: park lat/long for weather lookup

runners/
  run_model.py            # PATCHED: generalized run_model_AM_example.py — same math
  build_workbook.py       # PATCHED: generalized build_wb_AM_example.py — same styling
  build_backtest.py       # PATCHED: generalized build_backtest_csv_example.py
  add_line_movement.py    # PATCHED: generalized add_line_movement_example.py

.github/workflows/kprop_pipeline.yml   # NEW: the actual scheduler replacing Perplexity
requirements.txt                        # NEW
```
