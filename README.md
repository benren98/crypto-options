# VRP Monitor — Short Put BTC

Portfolio monitoring and management system for short BTC puts on Deribit, delta-hedged via BTC-PERPETUAL. Runs hourly via GitHub Actions with a live GitHub Pages dashboard.

---

## Architecture

```
greeks_hedge.py       — core engine: scan, entries, roll, hedge
pnl_monitor.py        — per-position PnL computation and CSV snapshots
generate_html.py      — dashboard HTML generation
positions.json        — portfolio state (source of truth: GitHub Gist)
positions_detail.json — live per-position data (pnl_monitor → generate_html)
scan_entry.json       — top 7 opportunities from last scan (greeks_hedge → generate_html)
```

Pipeline order: fetch Gist → pnl_monitor → greeks_hedge → generate_html → commit → push Gist.

---

## Strategy

Sell OTM BTC puts with short maturities (1–30 days), delta-hedged via a BTC-PERPETUAL short. The edge is the **Volatility Risk Premium (VRP)**: implied volatility (IV) is structurally higher than realised volatility (HV), making systematic option selling statistically profitable over time.

**Main risk**: a sharp downside move (crash, gap) where gamma spikes and delta moves faster than the hedge can follow.

---

## Option Selection

### Universe

| Parameter | Value |
|---|---|
| Type | OTM puts only |
| DTE | 1 – 30 days |
| Delta | 0 to −0.30 (exposure cap only, no floor) |
| Max bid/ask spread | 12% of mark |

### Composite score

Each option receives a score between 0 and 1:

```
score_raw = 0.40 × s_iv_hv + 0.30 × s_yield + 0.30 × s_skew
score     = score_raw × gamma_factor
```

All three components are option-specific (the DVOL rank, identical for every candidate in a scan, was moved out of the score and into the sizing multiplier — see Sizing).

**IV/HV component** — captures the volatility risk premium for this specific option:
```
HV_blend = 0.5 × HV_10d + 0.5 × HV_30d
s_iv_hv  = clamp(bid_IV / HV_blend − 1.0, 0, 1)
```
0 when bid IV = HV (no premium), 1 when bid IV = 2× HV. Uses `bid_iv` (IV implied by the bid price — the price we actually sell at), not mid IV. The blended HV keeps the responsiveness of the 10-day window while damping the cliff effect of a single large day entering/leaving it (HV_10d ranged 20–57% within one month). Weight: 40%.

**Risk-adjusted yield component** — annualised premium scaled by the strike's distance in realised vols:
```
yield_ann = bid_price / DTE_years
z         = OTM% / (HV_blend × √DTE_years)
s_yield   = min(1.0, (yield_ann × z) / 0.30)
```
`z` is the distance to the strike expressed in realised-vol standard deviations: a high yield close to the strike is worth less than a moderate yield far from it. Uses `bid_price` (the price we actually receive when selling). Weight: 30%.

**Skew component** — how rich the sold strike is relative to the ATM of the same expiry:
```
atm_IV = mark IV of the put whose strike is closest to spot (same expiry)
s_skew = clamp((bid_IV / atm_IV − 1) / 0.20, 0, 1)
```
Reaches 1 when the put trades 20% richer than ATM. Between two puts with equal overall scores, this favours the one the market overpays the most relative to the centre of the smile — exactly the premium the strategy sells. Weight: 30%.

**Gamma penalty** — discounts the raw score for high-gamma options:
```
gamma_pts    = gamma × spot × 0.01 × 100          (delta points per 1% spot move)
gamma_excess = max(0, gamma_pts − GAMMA_PENALTY_START)
gamma_factor = max(0, 1 − gamma_excess / (GAMMA_SCORE_CAP − GAMMA_PENALTY_START))
score        = score_raw × gamma_factor
```
No penalty below `GAMMA_PENALTY_START = 5 pts`. Linear discount from 5 pts (×1.0) to `GAMMA_SCORE_CAP = 10 pts` (×0.0, eliminated). This penalises short-dated or near-ATM options whose high gamma represents an outsized risk relative to the premium collected.

Examples: gamma = 5 pts → ×1.00 · gamma = 7.5 pts → ×0.50 · gamma ≥ 10 pts → eliminated.

### Score calibration — empirical basis (data as of 2026-06-12)

Each component's normalisation constant was calibrated against live Deribit data so the component actually spans its 0–1 range in the regimes we trade. Reference snapshots below — re-run the analysis scripts (`yield_range.py`, `ivhv_range.py`, `dvol_range.py`, `hv_range.py`) to recalibrate later.

**s_iv_hv — no explicit normalisation (cap at ratio 2.0×).** DVOL / HV_blend daily over 30 days (May–June 2026):

| Percentile | Ratio | Resulting s_iv_hv |
|---|---|---|
| Min | 0.91× | 0.00 |
| Median | 1.18× | 0.18 |
| P90 | 1.66× | 0.66 |
| Max (2026-05-13) | 1.81× | 0.81 |

The component spans 0 → 0.8 within a single month even with the blended HV, so no rescaling is needed. Per-option values run ~0.10–0.15 higher than this table because candidates use `bid_iv` which includes the put-skew premium on top of the ATM-level DVOL. `s_iv_hv = 0` across the board (as in early June 2026) is not a scale defect — it correctly signals a negative VRP regime (market realising more than implieds pay) where the strategy should not sell.

**s_yield — normalised at 30% annualised.** Distribution of raw annualised yields across 54 scannable OTM puts (3–25% OTM, 1–30 DTE, June 2026): min 0.5%, P25 6.4%, median 18.7%, P75 40.3%, P90 57.5%, max 113.5%. The original 20% norm saturated half the universe at 1.0 (non-discriminating); 60% (~P90) was considered but compressed existing scores too much; 30% was chosen as the compromise. Note the component is applied to `yield × z`, not raw yield, which pulls high-yield/near-strike candidates back down.

**s_skew — normalised at 20%.** Observed skew richness vs same-expiry ATM in the 4–13% OTM hunting zone (June 2026): 4.4% (3.7% OTM) → 18.9% (13.1% OTM). The 20% norm means the component spreads ~0.2–0.95 across the zone without saturating in normal regimes, while panic regimes (skew can reach 30–50%) saturate at 1.0 — correctly flagging "exceptionally rich skew, sell it".

**Gamma penalty thresholds (5 → 10 pts).** In the hunting zone, far-OTM medium-dated puts run 1–3 pts; near-ATM or short-dated puts run 5–10+ pts. The 5-pt start avoids penalising normal candidates; the 10-pt elimination kills only the genuinely dangerous gamma profiles.

**Entry threshold 0.45.** Calibrated so the implicit demand matches the previous scoring version: with a typical base of ~0.30 from the yield and skew components, crossing 0.45 requires `s_iv_hv ≈ 0.35–0.40`, i.e. **IV/HV ≈ 1.35–1.40** — the same implicit bar the old 0.58 threshold imposed before the DVOL rank was moved out of the score.

### Entry thresholds

All conditions must be met simultaneously for opportunistic entries:

| Condition | Threshold |
|---|---|
| Composite score (after gamma penalty) | ≥ 0.45 |
| IV/HV ratio (per option, bid IV) | ≥ 1.10 |
| Bid/ask spread | ≤ 12% of mark |
| Market condition | DVOL ≥ 35% |

### Sizing

The DVOL 30-day rank acts as an aggressiveness multiplier: full size when vol is at the top of its 30-day range, half size at the bottom.

```python
rank_mult = 0.5 + 0.5 × iv_rank          # iv_rank = DVOL position in 30d range [0..1]
contracts = round(score × rank_mult, 1)  # e.g. score 0.72, rank 0.8 → 0.6 BTC
contracts = max(0.1, contracts)
contracts = min(contracts, MAX_PORTFOLIO_BTC − used_btc)
```

Portfolio cap: **5 BTC notional total**. Sizing reflects conviction: a score of 0.6 opens 0.6 BTC, a score of 1.0 opens 1.0 BTC (1 Deribit contract = 1 BTC).

### Circuit breaker

De-risks the whole book on violent moves, re-enters when realized vol turns down while implieds are still rich. Calibrated by backtest (2023–2026 model backtest: max drawdown −19% for −7% PnL, same Sharpe, zero ITM expiries).

**Trigger** (checked every run):
```
spot move over 3 days < −10%   OR   DVOL change over 3 days > +12 pts
```
Downside only: an upward move melts short puts (their gamma fades as spot moves away from the strikes) and is harmless. Restricting to downside removed 6 of 15 backtest triggers (all pump false-alarms) with slightly better PnL and identical max drawdown. The DVOL leg is a near-free backstop for an implied-vol explosion without a spot move (priced-in event risk): it fired only twice in 2.7 years of history, both times alongside a spot move that had already triggered.
Action: buy back **all** positions at the ask, flatten the perp hedge (PnL realized), set `risk_off = true` in positions.json. No new entries while risk-off (including the always-≥1-position rule).

**Re-entry**:
```
HV_5d < HV_10d   AND   |spot move over 3 days| < 4%
```
The short realized vol turning back below the 10-day says the stress peak is behind; entries resume under normal scoring rules — typically into still-elevated implieds (post-stress premiums are the richest the strategy ever sells).

Constants: `CB_MOVE_3D_PCT = 10.0`, `CB_DVOL_3D_PTS = 12.0`, `CB_REENTRY_MOVE_PCT = 4.0`. Threshold sweep in `cb_sweep.py`: looser (8%) whipsaws (30 triggers), stricter (12–15%) fires after the damage and is worse than no breaker at all.

The dashboard shows the breaker state in the header (armed + margin vs thresholds, or RISK-OFF since timestamp) and draws the trigger levels on the spot chart (±10% vs 3 days ago) and the vol chart (DVOL 3d + 12 pts).

---

## Portfolio Management

### Limits

| Parameter | Value |
|---|---|
| Max total notional | 5 BTC |

### "Always in a position" guarantee

If the portfolio is empty (after a roll or on first run), the algo **always opens** the best scored candidate, even if market signal conditions are not met. If no liquid candidate is found (B/A > 12%), the spread filter is lifted to guarantee entry. Sizing still follows `round(score, 1)` with a 0.1 BTC minimum.

### Opportunistic entries

When total notional is below the cap (< 5 BTC), the algo attempts to open one additional position per run if the market signal is active and the best eligible candidate meets the minimum score.

**Diversification constraint**: on a given expiry, a new position is only allowed if `|delta_new − delta_existing| ≥ DELTA_MIN_SPACING (0.08)`. This prevents clustering of positions with similar strikes on the same expiry.

**Re-entry**: an instrument already held can be opened again if its current score exceeds its entry score by at least `ENTRY_SCORE_REENTRY_BOOST (0.05)`. This allows increasing conviction when IV rises further after the initial entry.

Only one opportunistic entry is made per pipeline run.

---

## Roll Logic

### Observation window

A roll enters its window once **DTE ≤ 1 day** (`ROLL_TRIGGER = 1.0`).

### Decision within the window

Once in the window, the roll is not automatic — it depends on **gamma in delta points**:

```
gamma_pts = gamma × spot × 0.01 × 100
```

This represents how many delta points (%) the position loses if spot moves 1%.

| Condition | Decision |
|---|---|
| DTE > 1d | HOLD — not yet in window |
| DTE ≤ 1d AND gamma > 6 pts/1% | **ROLL** — put too close to strike, ATM risk |
| DTE ≤ 1d AND gamma ≤ 6 pts/1% | **HOLD** — put sufficiently OTM, let it expire |

**Gamma roll threshold: 6 delta points / 1% move.**

Rationale: if an option with 1 day left still has high gamma, it is near the strike (ATM or slightly OTM). The risk of a gap down in the final hours is asymmetric. Low gamma means the put is deep OTM and will expire worthless — theta is still working, no need to roll.

### Automatic expiry

Positions whose expiry date has passed are automatically moved from `positions[]` to `history[]` at the start of each `run_once()` via `expire_positions()`.

---

## Delta Hedge

### Instrument

BTC-PERPETUAL short. The hedge targets a net delta of zero across the full portfolio (all open puts combined).

### Target hedge quantity

```
target_hedge_qty = −(net_delta_options × contracts)
```

Net options delta is the sum of deltas across all open positions. Since puts have negative delta, the hedge is a BTC-PERPETUAL short.

### Rebalancing threshold — IV-adjusted

The threshold is not fixed: it widens when IV rises (higher realised vol means rebalancing too often is costly in transaction fees).

```
threshold_pct = BASE_PCT × sqrt(IV_current / IV_ref)
threshold_pct = clamp(threshold_pct, 2%, 8%)
threshold_btc = threshold_pct / 100
```

| Parameter | Value |
|---|---|
| Base band (`BASE_PCT`) | 5% |
| Reference IV (`IV_REF`) | 70% |
| Lower bound | 2% |
| Upper bound | 8% |

**Examples:**
- IV = 70% → threshold = 5.0% (nominal)
- IV = 30% → threshold ≈ 3.3% (low vol, rebalance more often)
- IV = 120% → threshold ≈ 6.5% (high vol, tolerate more drift)

Rebalancing triggers when `|delta_drift| > threshold_btc`.

### Hedge VWAP

The weighted average entry price of the hedge is updated on each execution:
- **Additional short**: VWAP recalculated by weighted average
- **Partial buy-back**: entry VWAP unchanged, realised P&L recorded on the closed portion
- **Full close**: realised P&L on the entire position

---

## PnL Attribution

For each position, the option P&L is decomposed into 5 contributions:

| Component | Formula |
|---|---|
| Δ Delta | `\|delta\| × ΔSpot` |
| Γ Gamma | `0.5 × (−gamma) × ΔSpot²` |
| Θ Theta | `theta_daily_usd × days_held` |
| ν Vega | `(−vega) × ΔIV_pts` |
| Residual | `PnL_total − (delta + gamma + theta + vega)` |

The residual captures higher-order effects, frictions, and model error.

The bid/ask costs are shown separately:
- **Entry B/A cost**: sold at bid, not mid — locked in at trade entry
- **Exit B/A cost (estimated)**: additional cost to buy back at ask right now

---

## Global Parameters

```python
# Portfolio
MAX_PORTFOLIO_BTC        = 5.0   # max total notional (BTC)

# Scan
SCAN_TTE_MIN             = 1.0   # min DTE (days)
SCAN_TTE_MAX             = 30.0  # max DTE (days)
SCAN_DELTA_MIN           = -0.30 # exposure cap : no closer to ATM than -0.30
SCAN_DELTA_MAX           = 0.0   # no floor : far-OTM small-delta puts are eligible
BA_MAX_PCT               = 12.0  # max bid/ask spread (% of mark)

# Entry signal
ENTRY_SCORE_MIN          = 0.45  # minimum composite score (after gamma penalty) — recalibrated for score v2 (rank moved to sizing), implicitly demands IV/HV ≈ 1.35
ENTRY_IV_HV_MIN          = 1.10  # minimum bid IV/HV ratio (per option)

# Gamma penalty on score
GAMMA_PENALTY_START      = 5.0   # gamma_pts below which no penalty applies
GAMMA_SCORE_CAP          = 10.0  # gamma_pts at which score reaches 0

# Diversification & re-entry
DELTA_MIN_SPACING        = 0.08  # min |delta| gap between positions on same expiry
ENTRY_SCORE_REENTRY_BOOST= 0.05  # score improvement needed to re-enter a held instrument

# Roll
ROLL_TRIGGER             = 1.0   # DTE (days) to enter roll window
GAMMA_ROLL_THRESHOLD     = 6.0   # gamma_pts above which we roll

# Hedge
HEDGE_THRESHOLD_BASE_PCT = 5.0   # base rebalancing band (% delta)
HEDGE_IV_REF             = 70.0  # reference IV for calibration
# effective threshold clamped between 2% and 8%
```

---

## Dashboard

Available on the repo's GitHub Page. Updated hourly by GitHub Actions. Contains:

- **Header chips**: spot (with 1h/4h/1d moves), total P&L, min DTE, net delta + rebalancing threshold, total notional BTC, last transaction
- **Open positions**: strike, DTE, premium, mark/ask, IV, delta (BTC + %), gamma (BTC + pts/1%), vega, P&L, entry score, sizing
- **Net greeks**: delta/gamma (weighted averages) / vega/theta cumulated + hedge status + drift bar
- **Global P&L**: option breakdown (mid/mid + B/A entry cost) + hedge floating MtM + realised section (closed options + hedge rebalances)
- **PnL attribution per position**: delta/gamma/theta/vega/residual decomposition + B/A costs + theta/VRP capture
- **Entry opportunities**: top 7 scored candidates with score (+ raw score before gamma penalty), gamma pts/1%, bid premium/BTC, status flags (eligible / held / re-entry / filtered), market context and entry thresholds
- **Hedge history**: all BTC-PERPETUAL executions
- **Alerts**: roll, rebalancing, IV spike, stop-loss
- **Charts**: greeks (delta % and gamma pts as weighted averages), P&L and spot over time with strike levels as horizontal lines
