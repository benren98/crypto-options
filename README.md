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
scan_entry.json       — top 5 opportunities from last scan (greeks_hedge → generate_html)
```

Pipeline order: fetch Gist → pnl_monitor → greeks_hedge → generate_html → commit → push Gist.

---

## Strategy

Sell OTM BTC puts with short maturities (5–30 days), delta-hedged via a BTC-PERPETUAL short. The edge is the **Volatility Risk Premium (VRP)**: implied volatility (IV) is structurally higher than realised volatility (HV), making systematic option selling statistically profitable over time.

**Main risk**: a sharp downside move (crash, gap) where gamma spikes and delta moves faster than the hedge can follow.

---

## Option Selection

### Universe

| Parameter | Value |
|---|---|
| Type | OTM puts only |
| DTE | 5 – 30 days |
| Delta | −0.10 to −0.30 |
| Max bid/ask spread | 12% of mark |

### Composite score

Each option receives a score between 0 and 1:

```
score = 0.40 × s_iv_hv + 0.30 × s_rank + 0.50 × s_yield
```

**IV/HV component** — captures the volatility risk premium for this specific option:
```
s_iv_hv = clamp(mark_IV / HV_10d − 1.0, 0, 1)
```
0 when IV = HV (no premium), 1 when IV = 2× HV. Each option uses its own `mark_iv` (which includes the volatility skew). Weight: 40%.

**IV rank component** — measures where the market's overall vol level (DVOL) sits in its 30-day range:
```
s_rank = (DVOL_current − DVOL_min30d) / (DVOL_max30d − DVOL_min30d)
```
This is the same value for all candidates in a given scan — it is a market context metric, not option-specific. Weight: 30%.

Note: individual option `mark_iv` is not used here because OTM puts always have a higher implied vol than DVOL due to the skew, which would push the rank to 100% for every option.

**Yield component** — annualised premium normalised to 20% BTC/year:
```
s_yield = min(1.0, (mark / DTE_years) / 0.20)
```
Reaches 1 when the annualised yield hits 20% BTC. Weight: 30%.

### Entry thresholds

All conditions must be met simultaneously:

| Condition | Threshold |
|---|---|
| Composite score | ≥ 0.58 |
| IV/HV ratio (per option) | ≥ 1.10 |
| Bid/ask spread | ≤ 12% of mark |
| Market condition | DVOL ≥ 35% |

### Sizing

```python
contracts = round(score, 1)   # e.g. score 0.72 → 0.7 BTC
contracts = max(0.1, contracts)
contracts = min(contracts, MAX_PORTFOLIO_BTC − used_btc)
```

Portfolio cap: **3 BTC notional total**. Sizing reflects conviction: a score of 0.6 opens 0.6 BTC, a score of 1.0 opens 1.0 BTC (1 Deribit contract = 1 BTC).

---

## Portfolio Management

### Limits

| Parameter | Value |
|---|---|
| Max total notional | 3 BTC |

### "Always in a position" guarantee

If the portfolio is empty (after a roll or on first run), the algo **always opens** the best scored candidate, even if market signal conditions are not met. If no liquid candidate is found (B/A > 12%), the spread filter is lifted to guarantee entry.

### Opportunistic entries

When total notional is below the cap (< 3 BTC), the algo attempts to open an additional position on each run if the market signal is active and the best candidate (not already held) meets the minimum score. The top-5 opportunities shown in the dashboard always exclude instruments already in the portfolio.

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
MAX_PORTFOLIO_BTC  = 3.0     # max total notional (BTC)

# Scan
SCAN_TTE_MIN       = 5.0     # min DTE (days)
SCAN_TTE_MAX       = 30.0    # max DTE (days)
SCAN_DELTA_MIN     = -0.30   # min delta
SCAN_DELTA_MAX     = -0.10   # max delta
BA_MAX_PCT         = 12.0    # max bid/ask spread (% of mark)

# Entry signal
ENTRY_SCORE_MIN    = 0.58    # minimum composite score
ENTRY_IV_HV_MIN    = 1.10    # minimum IV/HV ratio (per option)

# Roll
ROLL_TRIGGER            = 1.0   # DTE (days) to enter roll window
GAMMA_ROLL_THRESHOLD    = 6.0   # gamma_pts above which we roll

# Hedge
HEDGE_THRESHOLD_BASE_PCT = 5.0  # base rebalancing band (% delta)
HEDGE_IV_REF             = 70.0 # reference IV for calibration
# effective threshold clamped between 2% and 8%
```

---

## Dashboard

Available on the repo's GitHub Page. Updated hourly by GitHub Actions. Contains:

- **Header chips**: spot (with 1h/4h/1d moves), total P&L, min DTE, net delta + rebalancing threshold, last transaction
- **Open positions**: strike, DTE, premium, mark/ask, IV, greeks, P&L, entry score, sizing
- **Net greeks**: delta/gamma/vega/theta cumulated + hedge status + drift bar
- **Global P&L**: option breakdown (mid/mid + B/A entry cost) + hedge floating MtM + realised section (closed options + hedge rebalances)
- **PnL attribution per position**: delta/gamma/theta/vega/residual decomposition + B/A costs + theta/VRP capture
- **Entry opportunities**: top 5 scored candidates (excluding held instruments) with market context and entry thresholds
- **Hedge history**: all BTC-PERPETUAL executions
- **Alerts**: roll, rebalancing, IV spike, stop-loss
- **Charts**: greeks, P&L and spot over time with strike levels as horizontal lines
