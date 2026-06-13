"""
backtest_eth.py â€” MÃªme stratÃ©gie VRP short put delta-hedgÃ©e, mais sur ETH.

DonnÃ©es rÃ©elles : spot ETH (perpetual daily) + DVOL ETH (Deribit).
ModÃ¨le de pricing identique Ã  backtest.py (Black-Scholes + skew linÃ©aire).

Sizing conservÃ© en BTC-Ã©quivalent (MAX_PORTFOLIO_BTC=5.0) :
    size_eth (contracts ETH) â†’ poids_BTC = size_eth Ã— S_eth / S_btc
La pÃ©nalitÃ©/cap portfolio compare donc les ETH en BTC courant.

Usage : python backtest_eth.py [--years 4]
"""
import sys, math, argparse
sys.path.insert(0, '.')
from datetime import datetime, timezone
from greeks_hedge import get, now_ms

# â”€â”€ ParamÃ¨tres stratÃ©gie (miroir greeks_hedge.py) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
ENTRY_SCORE_MIN   = 0.45
MAX_PORTFOLIO_BTC = 5.0      # cap en BTC-Ã©quivalent (ETH converti au taux du jour)
GAMMA_PEN_START   = 5.0
GAMMA_SCORE_CAP   = 10.0
DVOL_MIN          = 35.0
YIELD_NORM        = 0.30
SKEW_NORM         = 0.20
MIN_PREMIUM_USD   = 50.0

# â”€â”€ ParamÃ¨tres modÃ¨le ETH â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# ETH a typiquement un skew lÃ©gÃ¨rement plus pentu que BTC; on garde le mÃªme
# calibrage faute de donnÃ©es suffisantes â€” Ã  affiner si besoin.
SKEW_SLOPE        = 0.013
BA_HAIRCUT_VOLPTS = 1.5
FUNDING_DAILY     = 0.0001
TTE_CHOICES       = [3, 7, 14, 21]
DELTA_TARGETS     = [-0.05, -0.08, -0.12, -0.16, -0.20, -0.25]

N     = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
n_pdf = lambda x: math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

def bs_put(S, K, T, sigma):
    if T <= 0:
        return max(K - S, 0.0), -1.0 if S < K else 0.0, 0.0
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / sq
    d2 = d1 - sq
    return K * N(-d2) - S * N(-d1), N(d1) - 1.0, n_pdf(d1) / (S * sq)

def strike_for_delta(S, T, sigma_atm, target_delta):
    lo, hi = S * 0.40, S * 1.0
    K = 0.5 * (lo + hi)
    for _ in range(60):
        K = 0.5 * (lo + hi)
        otm = max((S - K) / S * 100, 0.0)
        sig = sigma_atm * (1 + SKEW_SLOPE * otm)
        _, d, _ = bs_put(S, K, T, sig)
        if abs(d - target_delta) < 0.0005:
            break
        if d < target_delta:
            hi = K
        else:
            lo = K
    return K

# â”€â”€ DonnÃ©es historiques ETH â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def fetch_history(years: float):
    end_ts   = now_ms()
    start_ts = end_ts - int(years * 365 * 24 * 3600 * 1000)
    # ETH spot
    spot_d = get('get_tradingview_chart_data', {
        'instrument_name': 'ETH-PERPETUAL',
        'start_timestamp': start_ts, 'end_timestamp': end_ts, 'resolution': '1D'})
    # ETH DVOL
    dvol_d = get('get_volatility_index_data', {
        'currency': 'ETH', 'start_timestamp': start_ts, 'end_timestamp': end_ts, 'resolution': '1D'})
    # BTC spot (pour conversion BTC-Ã©quivalent)
    btc_d = get('get_tradingview_chart_data', {
        'instrument_name': 'BTC-PERPETUAL',
        'start_timestamp': start_ts, 'end_timestamp': end_ts, 'resolution': '1D'})

    dvol_by_day = {datetime.fromtimestamp(r[0]/1000, tz=timezone.utc).date(): r[4]
                   for r in dvol_d['data']}
    btc_by_day  = {datetime.fromtimestamp(t/1000, tz=timezone.utc).date(): c
                   for t, c in zip(btc_d['ticks'], btc_d['close']) if c}

    days = []
    for tick, close in zip(spot_d['ticks'], spot_d['close']):
        d = datetime.fromtimestamp(tick/1000, tz=timezone.utc).date()
        if d in dvol_by_day and d in btc_by_day and close:
            days.append({'date': d, 'spot': close, 'dvol': dvol_by_day[d],
                         'btc': btc_by_day[d]})
    return days

def hv_from(closes, n):
    if len(closes) < n + 1:
        return None
    w = closes[-(n+1):]
    rets = [math.log(w[i]/w[i-1]) for i in range(1, len(w))]
    return math.sqrt(sum(r*r for r in rets)/len(rets)) * math.sqrt(365) * 100

# â”€â”€ Circuit breaker â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
CB_MOVE_3D_PCT  = 10.0
CB_DVOL_3D_PTS  = 12.0
CB_REENTRY_MOVE = 4.0

def rank_mult_linear(iv_rank: float) -> float:
    return 0.5 + 0.5 * iv_rank

def run(years: float = 4.0, always_one: bool = True, rank_mult=rank_mult_linear,
        circuit_breaker: bool = True, verbose: bool = False):
    days = fetch_history(years + 0.15)
    closes_hist = []
    positions   = []     # {strike, tte_left, contracts_eth, entry_premium_usd}
    hedge_qty   = 0.0    # ETH short (perp ETH)
    hedge_vwap  = 0.0
    cash        = 0.0
    equity_curve = []
    n_trades = n_expired_itm = 0
    worst_days = []
    notionals_usd = []
    dvol_30  = []
    dvol_hist = []
    risk_off = False
    n_cb_triggers = 0
    cb_days_off   = 0

    for day in days:
        S, dvol, S_btc = day['spot'], day['dvol'], day['btc']
        closes_hist.append(S)
        dvol_30.append(dvol)
        dvol_30  = dvol_30[-30:]
        dvol_hist.append(dvol)
        hv10, hv30 = hv_from(closes_hist, 10), hv_from(closes_hist, 30)
        if hv10 is None or hv30 is None or len(dvol_30) < 10:
            continue
        hv5      = hv_from(closes_hist, 5)
        hv_blend = 0.5 * hv10 + 0.5 * hv30
        iv_rank  = max(0.0, min(1.0, (dvol - min(dvol_30)) / max(max(dvol_30) - min(dvol_30), 5)))
        move_3d        = abs(S / closes_hist[-4] - 1) * 100 if len(closes_hist) >= 4 else 0.0
        move_3d_signed = (S / closes_hist[-4] - 1) * 100   if len(closes_hist) >= 4 else 0.0
        dvol_chg_3d    = dvol - dvol_hist[-4]               if len(dvol_hist)  >= 4 else 0.0

        # ratio ETH/BTC pour conversion portfolio
        eth_btc = S / S_btc   # 1 ETH = eth_btc BTC

        day_pnl = 0.0

        # â”€â”€ 0. Circuit breaker â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if circuit_breaker:
            if not risk_off and positions and (move_3d_signed < -CB_MOVE_3D_PCT or dvol_chg_3d > CB_DVOL_3D_PTS):
                for p in positions:
                    T   = p['tte_left'] / 365
                    otm = (S - p['strike']) / S * 100
                    sig = (dvol * (1 + SKEW_SLOPE * max(otm, 0)) + BA_HAIRCUT_VOLPTS) / 100
                    price, _, _ = bs_put(S, p['strike'], T, sig)
                    cash    += p['entry_premium_usd'] - price * p['contracts_eth']
                    day_pnl += p['entry_premium_usd'] - price * p['contracts_eth']
                positions = []
                if hedge_qty != 0:
                    cash    += hedge_qty * (hedge_vwap - S)
                    day_pnl += hedge_qty * (hedge_vwap - S)
                    hedge_qty, hedge_vwap = 0.0, 0.0
                risk_off = True
                n_cb_triggers += 1
            elif risk_off:
                cb_days_off += 1
                if hv5 is not None and hv5 < hv10 and move_3d < CB_REENTRY_MOVE:
                    risk_off = False

        # â”€â”€ 1. Expiration â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        still = []
        for p in positions:
            p['tte_left'] -= 1
            if p['tte_left'] <= 0:
                payoff   = max(p['strike'] - S, 0.0) * p['contracts_eth']
                cash    += p['entry_premium_usd'] - payoff
                day_pnl += p['entry_premium_usd'] - payoff
                if payoff > 0:
                    n_expired_itm += 1
            else:
                still.append(p)
        positions = still

        # â”€â”€ 2. Mark-to-model â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        net_delta  = 0.0
        mtm_value  = 0.0
        for p in positions:
            T   = p['tte_left'] / 365
            otm = (S - p['strike']) / S * 100
            sig = dvol/100 * (1 + SKEW_SLOPE * max(otm, 0))
            price, delta, _ = bs_put(S, p['strike'], T, sig)
            net_delta += delta * p['contracts_eth']
            mtm_value += price * p['contracts_eth']

        # â”€â”€ 3. Hedge (ETH-PERPETUAL) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        target_short = -net_delta
        drift = abs(target_short - hedge_qty)
        thr   = max(0.03, min(0.08, 0.05 * 60 / max(dvol, 20)))
        if drift > thr * max(sum(p['contracts_eth'] for p in positions), 1):
            dq = target_short - hedge_qty
            if hedge_qty != 0 and (dq * hedge_qty < 0):
                closed   = min(abs(dq), abs(hedge_qty)) * (1 if hedge_qty > 0 else -1)
                cash    += closed * (hedge_vwap - S)
                day_pnl += closed * (hedge_vwap - S)
            if target_short != 0:
                if hedge_qty * target_short > 0 and abs(target_short) > abs(hedge_qty):
                    add        = target_short - hedge_qty
                    hedge_vwap = (hedge_vwap * abs(hedge_qty) + S * abs(add)) / abs(target_short)
                elif hedge_qty * target_short <= 0:
                    hedge_vwap = S
            hedge_qty = target_short

        cash    -= abs(hedge_qty) * S * FUNDING_DAILY
        day_pnl -= abs(hedge_qty) * S * FUNDING_DAILY

        # â”€â”€ 4. EntrÃ©es â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Notionnel courant en BTC-Ã©quivalent
        used_btc  = sum(p['contracts_eth'] * eth_btc for p in positions)
        must_open = always_one and not positions and not risk_off
        if not risk_off and ((dvol >= DVOL_MIN and used_btc < MAX_PORTFOLIO_BTC) or must_open):
            best = None
            for tte in TTE_CHOICES:
                T = tte / 365
                for td in DELTA_TARGETS:
                    K   = strike_for_delta(S, T, dvol/100, td)
                    otm = (S - K) / S * 100
                    if otm < 2:
                        continue
                    mark_iv = dvol * (1 + SKEW_SLOPE * otm)
                    bid_iv  = mark_iv - BA_HAIRCUT_VOLPTS
                    price, delta, gamma = bs_put(S, K, T, bid_iv/100)
                    if price < MIN_PREMIUM_USD:
                        continue
                    yield_a = (price / S) / T
                    s_ivhv  = max(0.0, min(1.0, bid_iv / hv_blend - 1.0))
                    z       = (otm/100) / max(hv_blend/100 * math.sqrt(T), 1e-9)
                    s_yield = min(1.0, yield_a * z / YIELD_NORM)
                    skew    = bid_iv / dvol - 1.0
                    s_skew  = max(0.0, min(1.0, skew / SKEW_NORM))
                    g_pts   = gamma * S * 0.01 * 100
                    g_fac   = max(0.0, 1.0 - max(0.0, g_pts - GAMMA_PEN_START) / (GAMMA_SCORE_CAP - GAMMA_PEN_START))
                    score   = (0.40*s_ivhv + 0.30*s_yield + 0.30*s_skew) * g_fac
                    if best is None or score > best['score']:
                        best = {'score': score, 'K': K, 'tte': tte, 'price': price, 'otm': otm}

            ok = best and (best['score'] >= ENTRY_SCORE_MIN or must_open)
            if ok:
                # Size en ETH, convertie pour respecter le cap BTC
                size_eth = round(best['score'] * rank_mult(iv_rank), 1)
                # Convertir cap restant en ETH
                remaining_btc = MAX_PORTFOLIO_BTC - used_btc
                remaining_eth = remaining_btc / eth_btc if eth_btc > 0 else 0
                size_eth = max(0.1, min(size_eth, remaining_eth))
                if size_eth >= 0.1:
                    positions.append({
                        'strike': best['K'], 'tte_left': best['tte'],
                        'contracts_eth': size_eth,
                        'entry_premium_usd': best['price'] * size_eth,
                    })
                    n_trades += 1

        # â”€â”€ 5. Equity â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        open_prem  = sum(p['entry_premium_usd'] for p in positions)
        hedge_mtm  = hedge_qty * (hedge_vwap - S)
        equity     = cash + open_prem - mtm_value + hedge_mtm
        eq_prev    = equity_curve[-1][1] if equity_curve else equity
        equity_curve.append((day['date'], equity, equity - eq_prev, S, dvol))
        worst_days.append((equity - eq_prev, day['date'], S, dvol))
        notional_eth     = sum(p['contracts_eth'] for p in positions)
        notionals_usd.append(notional_eth * S)

    globals()['_LAST_RUN'] = {"curve": equity_curve, "notionals_usd": notionals_usd}

    # â”€â”€ Stats â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    eq    = [e[1] for e in equity_curve]
    rets  = [eq[i] - eq[i-1] for i in range(1, len(eq))]
    peak, max_dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        max_dd = max(max_dd, peak - v)
    mean_d = sum(rets)/len(rets)
    std_d  = (sum((r-mean_d)**2 for r in rets)/len(rets)) ** 0.5
    sharpe = mean_d / std_d * math.sqrt(365) if std_d > 0 else 0
    worst_days.sort()

    print(f"\n{'='*70}")
    print(f"  BACKTEST ETH  {equity_curve[0][0]} -> {equity_curve[-1][0]}  ({len(equity_curve)} jours)")
    print(f"  Circuit breaker : {'ON' if circuit_breaker else 'OFF'}  |  RÃ¨gle â‰¥1 : {'ON' if always_one else 'OFF'}")
    print(f"{'='*70}")
    print(f"  PnL final        : {eq[-1]:>12,.0f} $")
    print(f"  PnL annualisÃ©    : {eq[-1]/len(eq)*365:>12,.0f} $/an")
    print(f"  Max drawdown     : {max_dd:>12,.0f} $")
    print(f"  Sharpe (daily)   : {sharpe:>12.2f}")
    print(f"  Trades           : {n_trades}  |  expirations ITM : {n_expired_itm}")
    if circuit_breaker:
        print(f"  Circuit breaker  : {n_cb_triggers} dÃ©clenchements  |  {cb_days_off} jours risk-off")
    print(f"\n  10 pires jours :")
    for pnl, d, s, dv in worst_days[:10]:
        print(f"    {d}  {pnl:>10,.0f} $   spot ETH {s:>8,.0f}  DVOL {dv:.0f}%")
    print(f"\n  Equity annuelle :")
    by_year = {}
    for d, e, *_ in equity_curve:
        by_year[d.year] = e
    prev = 0
    for y in sorted(by_year):
        print(f"    {y} : {by_year[y]-prev:>+12,.0f} $")
        prev = by_year[y]
    return equity_curve

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--years', type=float, default=4.0)
    ap.add_argument('--no-cb', action='store_true')
    a = ap.parse_args()
    run(a.years, circuit_breaker=not a.no_cb)

