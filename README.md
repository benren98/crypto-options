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
| Min premium collected | $150 / BTC at bid (dust filter, BTC-specific) |
| Max bid/ask spread | 50% of mark (illiquidity backstop only) |

### Composite score

Each option receives a score between 0 and 1:

```
score_raw = 0.30 × s_iv_hv + 0.25 × s_yield + 0.45 × s_skew
score     = score_raw × gamma_factor
```

> **Weight calibration (skew-weighted, 4-year backtest).** Weights were retuned from the
> original 0.40/0.30/0.30 toward the **skew** component. Over-weighting `s_yield` is the most
> dangerous setting (it chases near-the-money high-premium options that blow up on gaps — in
> backtest it ballooned max drawdown to 18.7k\$); over-weighting `s_skew` selects further-OTM
> options where the market overpays most for crash fear (richer, less gap-sensitive). The
> retune (`0.30/0.25/0.45`) cut **max drawdown −20% at neutral PnL** (Calmar 2.23 → 2.77). See
> `backtest_scoring.py`.

All three components are option-specific (the DVOL rank, identical for every candidate in a scan, was moved out of the score and into the sizing multiplier — see Sizing).

**IV/HV component** — captures the volatility risk premium for this specific option:
```
HV_blend = 0.5 × HV_10d + 0.5 × HV_30d
s_iv_hv  = clamp(bid_IV / HV_blend − 1.0, 0, 1)
```
0 when bid IV = HV (no premium), 1 when bid IV = 2× HV. Uses `bid_iv` (IV implied by the bid price — the price we actually sell at), not mid IV. The blended HV keeps the responsiveness of the 10-day window while damping the cliff effect of a single large day entering/leaving it (HV_10d ranged 20–57% within one month). Weight: **30%**.

**Risk-adjusted yield component** — annualised premium scaled by the strike's distance in realised vols:
```
yield_ann = bid_price / DTE_years
z         = OTM% / (HV_blend × √DTE_years)
s_yield   = min(1.0, (yield_ann × z) / 0.30)
```
`z` is the distance to the strike expressed in realised-vol standard deviations: a high yield close to the strike is worth less than a moderate yield far from it. Uses `bid_price` (the price we actually receive when selling). Weight: **25%** (reduced — this is the most gap-dangerous component).

**Skew component** — how rich the sold strike is relative to the ATM of the same expiry:
```
atm_IV = mark IV of the put whose strike is closest to spot (same expiry)
s_skew = clamp((bid_IV / atm_IV − 1) / 0.20, 0, 1)
```
Reaches 1 when the put trades 20% richer than ATM. Between two puts with equal overall scores, this favours the one the market overpays the most relative to the centre of the smile — exactly the premium the strategy sells. Weight: **45%** (raised — steep-skew strikes sit further OTM and are less gap-sensitive).

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

**Entry threshold 0.50.** Raised from 0.45 alongside the skew-weighted score (the skew-heavy weighting shifts the score distribution upward, so a higher bar keeps the same selectivity). In backtest the 0.50 bar combined with the skew weighting and the disabled always-in rule (see below) cut max drawdown −28% at neutral PnL. Raising the threshold *without* the skew reweighting was worse — the lever works as a bundle.

### Entry thresholds

All conditions must be met simultaneously for opportunistic entries:

| Condition | Threshold |
|---|---|
| Composite score (after gamma penalty) | ≥ 0.50 |
| IV/HV ratio (per option, bid IV) | ≥ 1.10 |
| Premium collected at bid | ≥ $50 / BTC |
| Bid/ask spread | ≤ 50% of mark |
| Market condition | DVOL ≥ 35% |

The premium floor replaces the bid/ask spread as the real quality gate. A wide spread on a cheap short-dated option is only a few dollars in absolute terms — what matters is whether the premium collected is worth the margin and tail risk. The 50% B/A is now just a backstop against completely illiquid markets.

### Sizing

The DVOL 30-day rank acts as an aggressiveness multiplier: full size when vol is at the top of its 30-day range, half size at the bottom. The score enters with a **convexity exponent of 1.5** that concentrates capital on the best opportunities.

```python
rank_mult = 0.5 + 0.5 × iv_rank             # iv_rank = DVOL position in 30d range [0..1]
contracts = round(score**1.5 × rank_mult, 1) # convexity: weak setups shrink, strong ones don't
contracts = max(0.1, contracts)
contracts = min(contracts, MAX_PORTFOLIO_BTC − used_btc)
```

Portfolio cap: **5 BTC notional total** (1 Deribit contract = 1 BTC).

**Convexity calibration (`score^1.5`).** The exponent reshapes size non-uniformly:

| Score | Linear (old) | `^1.5` (new) | Reduction |
|-------|--------------|--------------|-----------|
| 1.00  | 1.00         | 1.00         | 0%        |
| 0.80  | 0.80         | 0.72         | −10%      |
| 0.60  | 0.60         | 0.46         | −23%      |
| 0.50 (at threshold) | 0.50 | 0.35 | **−29%**  |

Marginal setups near the 0.50 entry threshold — which cluster on the fragile days preceding gap-downs — get cut by ~29%, while high-conviction scores are barely touched. On the 4-year backtest (BTC, circuit breaker on) this lowers **max drawdown by 28% (9.8k → 7.1k$)** while *raising* PnL slightly (38.5k → 39.3k$): the trimmed capital was sitting on low-edge trades. Calmar improves 1.48 → 2.08, Sharpe 1.63 → 1.84.

`^2.0` was tested and overshoots — it starves capital deployment (avg notional 3.4 vs 3.7 BTC) and cuts PnL. `1.5` is the sweet spot. A per-portfolio **aggregate gamma budget** was also evaluated: it is gamma-specific (binds before the notional cap) and adds ~+2k$ PnL, but does *not* reduce drawdown further — the entire DD reduction comes from convexity — so it was left out to keep sizing to a single lever. Reducing the notional cap in high-DVOL regimes was tested and is counterproductive (high DVOL = richest premium; the circuit breaker already handles the gap risk). See `backtest_sizing.py` and `diag_gamma.py`.

### Circuit breaker (two-tier graduated)

De-risks the book in two stages on a down-move, re-enters when realized vol turns down while implieds are still rich. The graduated design beats a binary all-or-nothing breaker: it cushions sharp single-day gaps without fully exiting, and recovers fast.

**Tier 1 — partial de-risk** (`GRADUATED_CB = True`, checked every run):
```
spot move over 1 day < −5%   OR   spot move over 3 days < −6%
→ trim the whole book to 30% (buy back 70% at the ask)
→ cap new entries at 30% of MAX_PORTFOLIO_BTC until recovery
```
Recovery (full size restored):
```
|spot move over 3 days| < 3%
```
The 1-day leg is crisis-alpha: trimming right after a sharp single-day crash and re-entering at higher vol is *PnL-positive* on both BTC (+7.5%) and ETH (+11%), and on ETH it halves the drawdown. Adding the 3-day leg deepens the drawdown protection (−20% BTC / −50% ETH MaxDD) for a light PnL cost (−8.6% BTC). The trigger was selected by a broad sweep (`backtest_cb2.py`): DVOL-based tier-1 triggers were rejected (they trim during the richest selling moments).

**Tier 2 — full close** (hard backstop):
```
spot move over 3 days < −10%   OR   DVOL change over 3 days > +12 pts
```
Action: buy back **all** positions at the ask, flatten the perp hedge (PnL realized), set `risk_off = true`. No new entries while risk-off. Downside only: an upward move melts short puts (gamma fades as spot moves away) and is harmless. The DVOL leg backstops an implied-vol explosion without a spot move.

**Re-entry from full close**:
```
HV_5d < HV_10d   AND   |spot move over 3 days| < 4%
```
The short realized vol turning back below the 10-day says the stress peak is behind; entries resume into still-elevated implieds (the richest premiums the strategy ever sells).

Constants: tier 1 `CB_T1_MOVE_1D_PCT = 5.0`, `CB_T1_MOVE_3D_PCT = 6.0`, `CB_T1_KEEP = 0.30`, `CB_T1_RESTORE_MOVE_PCT = 3.0`; tier 2 `CB_MOVE_3D_PCT = 10.0`, `CB_DVOL_3D_PTS = 12.0`, `CB_REENTRY_MOVE_PCT = 4.0`. Set `GRADUATED_CB = False` to revert to the binary breaker. Backtests: `backtest_cb.py` (graduated calibration), `backtest_cb2.py` (trigger sweep), `cb_robustness.py` (ETH + per-year).

The dashboard shows the breaker state in the header (armed + margin vs thresholds, or RISK-OFF since timestamp) and draws the trigger levels on the spot chart (±10% vs 3 days ago) and the vol chart (DVOL 3d + 12 pts).

---

## Portfolio Management

### Limits

| Parameter | Value |
|---|---|
| Max total notional | 5 BTC |

### "Always in a position" guarantee — disabled by default (`ALWAYS_IN_POSITION = False`)

The forced-entry rule (open the best candidate on an empty book even if it fails the score/signal gate) is **off**. Staying flat on low-opportunity days, rather than forcing a weak sale, lowered max drawdown −28% at neutral PnL on the 4-year backtest (the forced entries clustered on fragile days). With it off, an empty book only opens when the best candidate clears `ENTRY_SCORE_MIN (0.50)` and the market signal is active — otherwise it sits in cash. Set `ALWAYS_IN_POSITION = True` to restore the guarantee. Sizing follows `round(score**1.5 × rank_mult, 1)` with a 0.1 BTC minimum.

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

## Approaches Tested and Rejected

A log of levers that were backtested and **did not work**, kept so they aren't re-tried and to justify the current design. Baseline for comparison is the production stack (skew-weighted score + convexity 1.5 + graduated CB): **BTC 4-year backtest ≈ PnL 38–40k$, MaxDD 3.4–4.1k$, Calmar 3.5–4.4.** Reproduce any of these with the named script.

### Drawdown-reduction attempts that failed

| Lever | Result | Why rejected | Script |
|---|---|---|---|
| **Trend / momentum sizing overlay** (cut size in down-trends) | Best variant Calmar 2.11 vs 2.23 baseline | Gaps start from *calm/uptrend* states (4 Aug 2024 gapped from normal), so a trend filter can't anticipate them; fires on false alarms that recover → bleeds PnL | `backtest_trend.py` |
| **Gamma / delta / TTE sweep** (earlier gamma penalty, cap delta, drop 3j) | Identical to baseline | The score already never selects gamma-heavy options (chosen options run g_pts ≈ 1–2, far below any penalty start) | `backtest_a3.py` |
| **Aggregate gamma budget** (cap portfolio dollar-gamma) | +2k PnL, **zero** DD benefit | Gamma-specific (binds before the notional cap) but only shifts PnL; the entire DD reduction comes from convexity | `backtest_sizing.py`, `diag_gamma.py` |
| **Permanent put spread** (buy a further-OTM put every trade) | PnL 39k → 7k | ~40k$ of premium drag over 4y to save a few k on the worst days; can't replace the CB; the put skew makes the protective leg expensive (paying VRP in reverse) | `backtest_putspread.py` |
| **Tactical hedge in the danger zone** (buy protection only near the CB) | Worst day −22% but **MaxDD worse** | Danger zone fires on false alarms → buy/sell whipsaw, net hedge PnL negative; only the tightest trigger was PnL-neutral and even then MaxDD rose | `backtest_tactical.py` |
| **Vol-target sizing** (size ∝ vol_target / HV) | Calmar 1.07–1.16 | Sizes *up* when realized vol is low — exactly the cheap-gamma setup that precedes gaps | `backtest_sizing.py` |
| **Reduce the notional cap in high-DVOL regimes** | PnL 19–26k, DD not improved | High DVOL = the richest premium; cutting size there throws away the best carry, and the CB already covers the gap | `backtest_sizing.py` |
| **DD-throttle** (cut size after losses) | PnL −20%+ | Pro-cyclical: sells low and misses the rebound | `backtest_sizing.py` |
| **`score^2.0` convexity** | Starves deployment (notional 3.4 vs 3.7), cuts PnL | Overshoots; `^1.5` is the sweet spot | `backtest_sizing.py` |

### Scoring / selection attempts that failed

| Lever | Result | Why rejected |
|---|---|---|
| **Yield-weighted score** (raise the yield weight) | MaxDD ballooned to **18.7k**, worst day −14k | Yield chases near-the-money high-premium options that blow up on gaps — it is the most dangerous component, hence *reduced* to 0.25 | `backtest_scoring.py` |
| **Raise entry threshold alone** (0.45 → 0.50 without the skew reweighting) | Worse PnL and DD | The 0.50 bar only works bundled with the skew-weighted score; in isolation it starves entries | `backtest_scoring.py` |
| **DVOL-based tier-1 CB trigger** (trim on a DVOL spike) | MaxDD 7.5–8.7k | Trims during the richest selling moments; move-based triggers win | `backtest_cb2.py` |

### Premium / tenor attempts that failed

| Lever | Result | Why rejected |
|---|---|---|
| **Lower `MIN_PREMIUM_USD` to 30$ / 10$** | Identical to 50$ | Non-binding: the score never picks an option in that premium band anyway | `backtest_premium.py` |
| **Adjust or disable the gamma penalty** (to admit short tenors) | Completely inert — identical even fully off | The penalty was never the binding constraint; the score itself excludes short tenors | `backtest_gamma.py` |
| **Short tenors for theta** (force 3–7j DTE) | 3j-only Calmar **0.41**, 7j-only 1.68 vs 21j-only 3.93 | High theta does *not* compensate the gamma/gap risk — the delta hedge can't keep up on big moves; the score correctly sells 21j | `backtest_gamma.py` |

### Other underlyings

| Lever | Result | Why rejected |
|---|---|---|
| **ETH-only** | PnL ~12× lower than BTC, more CB triggers | ETH DVOL is noisier with more downside whipsaws over this period | `backtest_eth.py` |
| **BTC + ETH mixed** (best score across both) | Better Sharpe but PnL halved | The mix picks ETH too often and dilutes profitability | `backtest_multi.py` |

> Note: several of these (vol-target, cap-reduction-in-stress, DVOL-trim, short-tenor theta) share one root cause — **high volatility is when this strategy earns the most**, so any lever that reduces exposure *because* vol is high fights the edge. Gap protection belongs in the event-triggered circuit breaker (near-zero carry cost), not in always-on de-risking.

---

## Real Data Collection & Model-Risk Reduction

The backtest reconstructs option prices from a Black-Scholes model with a **linear** skew
(`IV(K) = DVOL × (1 + 0.013 × OTM%)`). This is model risk: the *real* put skew is steeper and
**convex** — a live snapshot showed a 22%-OTM put at IV 79.8% vs ATM 40.7% (ratio **1.96**),
where the linear model predicts only 1.29. To remove this risk over time, the system collects
real data forward and auto-substitutes it into the backtest.

**Collection** — `vol_surface_logger.py` records one snapshot per UTC day (deduplicated) of the
real put smile: for the nearest 3 expiries in the 4–35 DTE window, every OTM strike's `mark_iv`,
`bid_iv`, delta, gamma, bid/ask, OI and 24h volume → `vol_surface.jsonl`. Runs in the hourly
Actions pipeline (writes only the first run of each day).

**Auto-switch trigger** — `backtest.py` prices via `iv_pct(S, K, dvol, date, dte)`:
- if the date is **covered** by the recorded surface → use the real interpolated IV
  (`vol_surface_data.iv_for`, linear in moneyness, nearest expiry in DTE);
- otherwise → fall back to the model.

This is backward-compatible: until the dataset covers backtest dates, every call hits the model and
results are unchanged. As the dataset grows, the recent backtest window silently becomes real-data-based,
**from the start of recording**, with zero code change. `USE_REAL_SURFACE = False` disables it.

**Fit & project to the past** — `fit_vol_model.py` fits a quadratic skew on the real surfaces,
`IV(K)/IV_ATM = 1 + a·OTM% + b·OTM²` (the `b` term captures the convexity the linear model misses),
and writes `vol_model_fit.json`. `backtest.py` reads it at import and replaces the linear `0.013`
with the fitted curve **for the model (pre-recording) portion** — i.e. it projects the real-shaped
skew backward in time. It writes the file only after **≥15 days** are collected (no-op until then,
so the default linear skew stands). The fit step runs in Actions after the logger.

Roadmap: collect ~2–3 weeks → the fit auto-activates and the projected skew improves the historical
backtest; collect longer → the recent backtest window runs entirely on real recorded IVs, eliminating
model risk for that period. Scripts: `vol_surface_logger.py`, `vol_surface_data.py`, `fit_vol_model.py`.

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
BA_MAX_PCT               = 50.0  # max bid/ask spread — illiquidity backstop only
MIN_PREMIUM_USD          = 150.0 # min premium at bid ($/BTC) — dust filter; backtest Calmar 3.56→4.40, DD −17%
                                 # (absolute $ floor, calibrated for BTC — ETH would need a relative % floor)

# Score weights (skew-weighted, C1)
SCORE_W_IVHV             = 0.30  # VRP (IV/HV) weight
SCORE_W_YIELD            = 0.25  # risk-adjusted yield weight (reduced — most gap-dangerous)
SCORE_W_SKEW             = 0.45  # skew vs ATM weight (raised — further-OTM, less gap-sensitive)

# Entry signal
ENTRY_SCORE_MIN          = 0.50  # minimum composite score (raised with skew weighting)
ALWAYS_IN_POSITION       = False # C2: do NOT force entry on an empty book if nothing clears the gate
ENTRY_IV_HV_MIN          = 1.10  # minimum bid IV/HV ratio (per option)

# Gamma penalty on score
GAMMA_PENALTY_START      = 5.0   # gamma_pts below which no penalty applies
GAMMA_SCORE_CAP          = 10.0  # gamma_pts at which score reaches 0

# Diversification & re-entry
DELTA_MIN_SPACING        = 0.08  # min |delta| gap between positions on same expiry
ENTRY_SCORE_REENTRY_BOOST= 0.05  # score improvement needed to re-enter a held instrument

# Sizing
SIZE_CONVEXITY           = 1.5   # size ∝ score^1.5 (concentrates capital on best scores)

# Circuit breaker — tier 1 (graduated de-risk)
GRADUATED_CB             = True  # enable the partial-trim tier before full close
CB_T1_MOVE_1D_PCT        = 5.0   # trim if 1-day spot drop > 5%
CB_T1_MOVE_3D_PCT        = 6.0   # or 3-day drop > 6%
CB_T1_KEEP               = 0.30  # fraction of book kept on trim
CB_T1_RESTORE_MOVE_PCT   = 3.0   # restore full size when |3-day move| < 3%
# Circuit breaker — tier 2 (full close)
CB_MOVE_3D_PCT           = 10.0  # full close if 3-day drop > 10%
CB_DVOL_3D_PTS           = 12.0  # or DVOL +12 pts in 3 days
CB_REENTRY_MOVE_PCT      = 4.0   # re-entry (from full close): |3-day move| < 4% AND HV5 < HV10

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
