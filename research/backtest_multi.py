"""
backtest_multi.py â€” Backtest mixte BTC + ETH : on prend le meilleur score
sur les deux sous-jacents Ã  chaque scan journalier.

Portfolio cap : MAX_PORTFOLIO_BTC = 5.0 BTC-Ã©quivalent.
    - position BTC : 1 contract = 1 BTC
    - position ETH : poids BTC = contracts_eth Ã— S_eth / S_btc

Le hedge est gÃ©rÃ© par sous-jacent (BTC-PERPETUAL ou ETH-PERPETUAL).
Sizing : mÃªme logique que backtest.py, exprimÃ© dans l'unitÃ© native de l'asset,
puis contraint par le cap rÃ©siduel converti.

Usage : python backtest_multi.py [--years 4]
"""
import sys, math, argparse
sys.path.insert(0, '.')
from datetime import datetime, timezone
from greeks_hedge import get, now_ms

# â”€â”€ ParamÃ¨tres â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
ENTRY_SCORE_MIN   = 0.45
MAX_PORTFOLIO_BTC = 5.0
GAMMA_PEN_START   = 5.0
GAMMA_SCORE_CAP   = 10.0
DVOL_MIN          = 35.0
YIELD_NORM        = 0.30
SKEW_NORM         = 0.20
MIN_PREMIUM_USD   = 50.0
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
    for _ in range(60):
        K   = 0.5 * (lo + hi)
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

def rank_mult_linear(iv_rank: float) -> float:
    return 0.5 + 0.5 * iv_rank

# â”€â”€ DonnÃ©es â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def fetch_history(years: float):
    """RÃ©cupÃ¨re BTC et ETH alignÃ©s sur les mÃªmes dates."""
    end_ts   = now_ms()
    start_ts = end_ts - int(years * 365 * 24 * 3600 * 1000)

    btc_spot = get('get_tradingview_chart_data', {
        'instrument_name': 'BTC-PERPETUAL',
        'start_timestamp': start_ts, 'end_timestamp': end_ts, 'resolution': '1D'})
    eth_spot = get('get_tradingview_chart_data', {
        'instrument_name': 'ETH-PERPETUAL',
        'start_timestamp': start_ts, 'end_timestamp': end_ts, 'resolution': '1D'})
    btc_dvol = get('get_volatility_index_data', {
        'currency': 'BTC', 'start_timestamp': start_ts, 'end_timestamp': end_ts, 'resolution': '1D'})
    eth_dvol = get('get_volatility_index_data', {
        'currency': 'ETH', 'start_timestamp': start_ts, 'end_timestamp': end_ts, 'resolution': '1D'})

    def to_dict(spot_d, dvol_d):
        dv = {datetime.fromtimestamp(r[0]/1000, tz=timezone.utc).date(): r[4]
              for r in dvol_d['data']}
        sp = {datetime.fromtimestamp(t/1000, tz=timezone.utc).date(): c
              for t, c in zip(spot_d['ticks'], spot_d['close']) if c}
        return sp, dv

    btc_sp, btc_dv = to_dict(btc_spot, btc_dvol)
    eth_sp, eth_dv = to_dict(eth_spot, eth_dvol)

    common = sorted(set(btc_sp) & set(eth_sp) & set(btc_dv) & set(eth_dv))
    return [{'date': d,
             'btc_spot': btc_sp[d], 'btc_dvol': btc_dv[d],
             'eth_spot': eth_sp[d], 'eth_dvol': eth_dv[d]}
            for d in common]

def hv_from(closes, n):
    if len(closes) < n + 1:
        return None
    w    = closes[-(n+1):]
    rets = [math.log(w[i]/w[i-1]) for i in range(1, len(w))]
    return math.sqrt(sum(r*r for r in rets)/len(rets)) * math.sqrt(365) * 100

# â”€â”€ Circuit breaker â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
CB_MOVE_3D_PCT  = 10.0
CB_DVOL_3D_PTS  = 12.0
CB_REENTRY_MOVE = 4.0

def scan_candidates(S, dvol, hv_blend, tte_list, delta_list):
    """Retourne liste de candidats scorÃ©s pour un sous-jacent."""
    candidates = []
    for tte in tte_list:
        T = tte / 365
        for td in delta_list:
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
            candidates.append({'score': score, 'K': K, 'tte': tte, 'price': price, 'otm': otm})
    return candidates

def run(years: float = 4.0, always_one: bool = True, rank_mult=rank_mult_linear,
        circuit_breaker: bool = True, verbose: bool = False):
    days = fetch_history(years + 0.15)

    btc_closes, eth_closes = [], []
    positions = []  # {asset:'BTC'|'ETH', strike, tte_left, contracts, entry_premium_usd}
    hedges    = {'BTC': {'qty': 0.0, 'vwap': 0.0},
                 'ETH': {'qty': 0.0, 'vwap': 0.0}}
    cash      = 0.0
    equity_curve  = []
    n_trades = n_expired_itm = 0
    worst_days    = []
    notionals_usd = []
    btc_dvol_30   = []
    eth_dvol_30   = []
    btc_dvol_hist = []
    eth_dvol_hist = []
    risk_off      = False
    n_cb_triggers = 0
    cb_days_off   = 0

    for day in days:
        S_btc = day['btc_spot']
        S_eth = day['eth_spot']
        dv_btc = day['btc_dvol']
        dv_eth = day['eth_dvol']
        eth_btc = S_eth / S_btc   # 1 ETH en BTC

        btc_closes.append(S_btc);  eth_closes.append(S_eth)
        btc_dvol_30.append(dv_btc); btc_dvol_30 = btc_dvol_30[-30:]
        eth_dvol_30.append(dv_eth); eth_dvol_30 = eth_dvol_30[-30:]
        btc_dvol_hist.append(dv_btc); eth_dvol_hist.append(dv_eth)

        btc_hv10 = hv_from(btc_closes, 10); btc_hv30 = hv_from(btc_closes, 30)
        eth_hv10 = hv_from(eth_closes, 10); eth_hv30 = hv_from(eth_closes, 30)
        if any(x is None for x in [btc_hv10, btc_hv30, eth_hv10, eth_hv30]):
            continue
        if len(btc_dvol_30) < 10:
            continue

        btc_hv5   = hv_from(btc_closes, 5)
        eth_hv5   = hv_from(eth_closes, 5)
        btc_blend = 0.5 * btc_hv10 + 0.5 * btc_hv30
        eth_blend = 0.5 * eth_hv10 + 0.5 * eth_hv30

        btc_rank = max(0.0, min(1.0, (dv_btc - min(btc_dvol_30)) / max(max(btc_dvol_30) - min(btc_dvol_30), 5)))
        eth_rank = max(0.0, min(1.0, (dv_eth - min(eth_dvol_30)) / max(max(eth_dvol_30) - min(eth_dvol_30), 5)))

        btc_move3s = (S_btc / btc_closes[-4] - 1)*100 if len(btc_closes) >= 4 else 0.0
        eth_move3s = (S_eth / eth_closes[-4] - 1)*100 if len(eth_closes) >= 4 else 0.0
        btc_move3  = abs(btc_move3s)
        eth_move3  = abs(eth_move3s)
        btc_dchg3  = dv_btc - btc_dvol_hist[-4] if len(btc_dvol_hist) >= 4 else 0.0
        eth_dchg3  = dv_eth - eth_dvol_hist[-4] if len(eth_dvol_hist) >= 4 else 0.0

        def spot(asset): return S_btc if asset == 'BTC' else S_eth
        day_pnl = 0.0

        # â”€â”€ 0. Circuit breaker : dÃ©clenchÃ© si BTC OU ETH dÃ©passe le seuil â”€â”€â”€â”€
        if circuit_breaker:
            cb_trigger = ((btc_move3s < -CB_MOVE_3D_PCT or btc_dchg3 > CB_DVOL_3D_PTS) or
                          (eth_move3s < -CB_MOVE_3D_PCT or eth_dchg3 > CB_DVOL_3D_PTS))
            if not risk_off and positions and cb_trigger:
                for p in positions:
                    S_  = spot(p['asset'])
                    dv_ = dv_btc if p['asset'] == 'BTC' else dv_eth
                    T   = p['tte_left'] / 365
                    otm = (S_ - p['strike']) / S_ * 100
                    sig = (dv_ * (1 + SKEW_SLOPE * max(otm,0)) + BA_HAIRCUT_VOLPTS) / 100
                    price, _, _ = bs_put(S_, p['strike'], T, sig)
                    cash    += p['entry_premium_usd'] - price * p['contracts']
                    day_pnl += p['entry_premium_usd'] - price * p['contracts']
                positions = []
                for asset, h in hedges.items():
                    S_ = S_btc if asset == 'BTC' else S_eth
                    if h['qty'] != 0:
                        cash    += h['qty'] * (h['vwap'] - S_)
                        day_pnl += h['qty'] * (h['vwap'] - S_)
                        h['qty'] = 0.0; h['vwap'] = 0.0
                risk_off = True
                n_cb_triggers += 1
            elif risk_off:
                cb_days_off += 1
                # Re-entrÃ©e : les deux actifs stabilisÃ©s
                btc_ok = btc_hv5 is not None and btc_hv5 < btc_hv10 and btc_move3 < CB_REENTRY_MOVE
                eth_ok = eth_hv5 is not None and eth_hv5 < eth_hv10 and eth_move3 < CB_REENTRY_MOVE
                if btc_ok and eth_ok:
                    risk_off = False

        # â”€â”€ 1. Expiration â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        still = []
        for p in positions:
            p['tte_left'] -= 1
            S_  = spot(p['asset'])
            if p['tte_left'] <= 0:
                payoff   = max(p['strike'] - S_, 0.0) * p['contracts']
                cash    += p['entry_premium_usd'] - payoff
                day_pnl += p['entry_premium_usd'] - payoff
                if payoff > 0:
                    n_expired_itm += 1
            else:
                still.append(p)
        positions = still

        # â”€â”€ 2. Mark-to-model + delta par asset â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        net_delta_btc = 0.0; net_delta_eth = 0.0; mtm_value = 0.0
        for p in positions:
            S_  = spot(p['asset'])
            dv_ = dv_btc if p['asset'] == 'BTC' else dv_eth
            T   = p['tte_left'] / 365
            otm = (S_ - p['strike']) / S_ * 100
            sig = dv_/100 * (1 + SKEW_SLOPE * max(otm,0))
            price, delta, _ = bs_put(S_, p['strike'], T, sig)
            if p['asset'] == 'BTC':
                net_delta_btc += delta * p['contracts']
            else:
                net_delta_eth += delta * p['contracts']
            mtm_value += price * p['contracts']

        # â”€â”€ 3. Hedge par asset â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        for asset, net_d, S_, dv_ in [('BTC', net_delta_btc, S_btc, dv_btc),
                                       ('ETH', net_delta_eth, S_eth, dv_eth)]:
            h = hedges[asset]
            target = -net_d
            n_pos  = sum(p['contracts'] for p in positions if p['asset'] == asset)
            drift  = abs(target - h['qty'])
            thr    = max(0.03, min(0.08, 0.05 * 60 / max(dv_, 20)))
            if drift > thr * max(n_pos, 1):
                dq = target - h['qty']
                if h['qty'] != 0 and dq * h['qty'] < 0:
                    closed   = min(abs(dq), abs(h['qty'])) * (1 if h['qty'] > 0 else -1)
                    cash    += closed * (h['vwap'] - S_)
                    day_pnl += closed * (h['vwap'] - S_)
                if target != 0:
                    if h['qty'] * target > 0 and abs(target) > abs(h['qty']):
                        add       = target - h['qty']
                        h['vwap'] = (h['vwap'] * abs(h['qty']) + S_ * abs(add)) / abs(target)
                    elif h['qty'] * target <= 0:
                        h['vwap'] = S_
                h['qty'] = target
            cash    -= abs(h['qty']) * S_ * FUNDING_DAILY
            day_pnl -= abs(h['qty']) * S_ * FUNDING_DAILY

        # â”€â”€ 4. EntrÃ©es â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Notionnel courant en BTC-Ã©quivalent
        def btc_equiv(p):
            return p['contracts'] if p['asset'] == 'BTC' else p['contracts'] * eth_btc

        used_btc  = sum(btc_equiv(p) for p in positions)
        must_open = always_one and not positions and not risk_off

        if not risk_off and ((used_btc < MAX_PORTFOLIO_BTC) or must_open):
            all_cands = []
            # Candidats BTC
            if dv_btc >= DVOL_MIN or must_open:
                for c in scan_candidates(S_btc, dv_btc, btc_blend, TTE_CHOICES, DELTA_TARGETS):
                    c['asset'] = 'BTC'; c['iv_rank'] = btc_rank
                    all_cands.append(c)
            # Candidats ETH
            if dv_eth >= DVOL_MIN or must_open:
                for c in scan_candidates(S_eth, dv_eth, eth_blend, TTE_CHOICES, DELTA_TARGETS):
                    c['asset'] = 'ETH'; c['iv_rank'] = eth_rank
                    all_cands.append(c)

            if all_cands:
                best = max(all_cands, key=lambda c: c['score'])
                ok   = best['score'] >= ENTRY_SCORE_MIN or must_open
                if ok:
                    asset  = best['asset']
                    ir     = best['iv_rank']
                    # Size en unitÃ© native, contrainte par cap rÃ©siduel BTC
                    size   = round(best['score'] * rank_mult(ir), 1)
                    remaining_btc = MAX_PORTFOLIO_BTC - used_btc
                    if asset == 'ETH':
                        max_native = remaining_btc / eth_btc if eth_btc > 0 else 0
                    else:
                        max_native = remaining_btc
                    size = max(0.1, min(size, max_native))
                    if size >= 0.1:
                        positions.append({
                            'asset': asset, 'strike': best['K'], 'tte_left': best['tte'],
                            'contracts': size,
                            'entry_premium_usd': best['price'] * size,
                        })
                        n_trades += 1

        # â”€â”€ 5. Equity â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        open_prem = sum(p['entry_premium_usd'] for p in positions)
        hedge_mtm = (hedges['BTC']['qty'] * (hedges['BTC']['vwap'] - S_btc) +
                     hedges['ETH']['qty'] * (hedges['ETH']['vwap'] - S_eth))
        equity    = cash + open_prem - mtm_value + hedge_mtm
        eq_prev   = equity_curve[-1][1] if equity_curve else equity
        equity_curve.append((day['date'], equity, equity - eq_prev, S_btc, dv_btc))
        worst_days.append((equity - eq_prev, day['date'], S_btc, dv_btc))

        not_usd   = sum(btc_equiv(p) * S_btc for p in positions)
        notionals_usd.append(not_usd)

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

    # RÃ©partition BTC/ETH sur le total des trades â€” reconstituÃ©e sur les positions
    # (simplifiÃ© : on dÃ©duit du nombre de positions actives par asset dans le temps,
    #  ce qui nÃ©cessite un tracking fin â€” on indique juste le total trades par asset ici)
    print(f"\n{'='*70}")
    print(f"  BACKTEST MULTI (BTC+ETH)  {equity_curve[0][0]} -> {equity_curve[-1][0]}")
    print(f"  Circuit breaker : {'ON' if circuit_breaker else 'OFF'}  |  RÃ¨gle â‰¥1 : {'ON' if always_one else 'OFF'}")
    print(f"{'='*70}")
    print(f"  PnL final        : {eq[-1]:>12,.0f} $")
    print(f"  PnL annualisÃ©    : {eq[-1]/len(eq)*365:>12,.0f} $/an")
    print(f"  Max drawdown     : {max_dd:>12,.0f} $")
    print(f"  Sharpe (daily)   : {sharpe:>12.2f}")
    print(f"  Trades total     : {n_trades}  |  expirations ITM : {n_expired_itm}")
    if circuit_breaker:
        print(f"  Circuit breaker  : {n_cb_triggers} dÃ©clenchements  |  {cb_days_off} jours risk-off")
    print(f"\n  10 pires jours :")
    for pnl, d, s, dv in worst_days[:10]:
        print(f"    {d}  {pnl:>10,.0f} $   BTC {s:>10,.0f}  DVOL BTC {dv:.0f}%")
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
    a  = ap.parse_args()
    run(a.years, circuit_breaker=not a.no_cb)

