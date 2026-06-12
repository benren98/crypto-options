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
| Delta | −0.10 to −0.30 |
| Max bid/ask spread | 12% of mark |

### Composite score

Each option receives a score between 0 and 1:

```
score_raw = 0.40 × s_iv_hv + 0.30 × s_rank + 0.30 × s_yield
score     = score_raw × gamma_factor
```

**IV/HV component** — captures the volatility risk premium for this specific option:
```
s_iv_hv = clamp(bid_IV / HV_10d − 1.0, 0, 1)
```
0 when bid IV = HV (no premium), 1 when bid IV = 2× HV. Uses `bid_iv` (IV implied by the bid price — the price we actually sell at), not mid IV. Weight: 40%.

**IV rank component** — measures where the market's overall vol level (DVOL index) sits in its 30-day range:
```
s_rank = (DVOL_current − DVOL_min30d) / (DVOL_max30d − DVOL_min30d)
```
`DVOL_current` is fetched live from the Deribit volatility index (same source as the 30-day range), not from an ATM option mark IV. This is the same value for all candidates in a given scan — it is a market context metric, not option-specific. Weight: 30%.

**Yield component** — annualised premium normalised to 30% BTC/year:
```
s_yield = min(1.0, (bid_price / DTE_years) / 0.30)
```
Uses `bid_price` (the price we actually receive when selling). Reaches 1 when the annualised yield hits 30% BTC — the previous 20% cap saturated for half the scannable universe (median yield ≈ 19%), making the component non-discriminating. Weight: 30%.

**Gamma penalty** — discounts the raw score for high-gamma options:
```
gamma_pts    = gamma × spot × 0.01 × 100          (delta points per 1% spot move)
gamma_excess = max(0, gamma_pts − GAMMA_PENALTY_START)
gamma_factor = max(0, 1 − gamma_excess / (GAMMA_SCORE_CAP − GAMMA_PENALTY_START))
score        = score_raw × gamma_factor
```
No penalty below `GAMMA_PENALTY_START = 5 pts`. Linear discount from 5 pts (×1.0) to `GAMMA_SCORE_CAP = 10 pts` (×0.0, eliminated). This penalises short-dated or near-ATM options whose high gamma represents an outsized risk relative to the premium collected.

Examples: gamma = 5 pts → ×1.00 · gamma = 7.5 pts → ×0.50 · gamma ≥ 10 pts → eliminated.

### Entry thresholds

All conditions must be met simultaneously for opportunistic entries:

| Condition | Threshold |
|---|---|
| Composite score (after gamma penalty) | ≥ 0.58 |
| IV/HV ratio (per option, bid IV) | ≥ 1.10 |
| Bid/ask spread | ≤ 12% of mark |
| Market condition | DVOL ≥ 35% |

### Sizing

```python
contracts = round(score, 1)   # e.g. score 0.72 → 0.7 BTC
contracts = max(0.1, contracts)
contracts = min(contracts, MAX_PORTFOLIO_BTC − used_btc)
```

Portfolio cap: **5 BTC notional total**. Sizing reflects conviction: a score of 0.6 opens 0.6 BTC, a score of 1.0 opens 1.0 BTC (1 Deribit contract = 1 BTC).

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
SCAN_DELTA_MIN           = -0.30 # min delta
SCAN_DELTA_MAX           = -0.10 # max delta
BA_MAX_PCT               = 12.0  # max bid/ask spread (% of mark)

# Entry signal
ENTRY_SCORE_MIN          = 0.58  # minimum composite score (after gamma penalty)
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
