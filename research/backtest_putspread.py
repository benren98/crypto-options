"""
backtest_putspread.py — Variante PUT SPREAD : on vend le put scoré (jambe courte)
et on achète un put plus OTM (jambe longue, même échéance) pour racheter du gamma
baissier et plafonner la queue.

Comparé au put nu + circuit breaker (production actuelle). On teste plusieurs
largeurs (delta de la jambe longue) et surtout AVEC vs SANS circuit breaker —
l'hypothèse étant que le spread, en bornant la perte max, peut remplacer le CB.

Conventions (miroir backtest.py) :
    - jambe courte vendue au bid  (mark_iv − haircut)
    - jambe longue achetée à l'ask (mark_iv + haircut)
    - delta net du spread (convention long-option) = (Δ_court − Δ_long)·n
      → target_short = −net_delta, hedge perp identique
    - rachat (buyback) = (P_court − P_long)·n
    - payoff à l'échéance = (max(K_court−S,0) − max(K_long−S,0))·n  (perte plafonnée)

Usage : python backtest_putspread.py [--years 4]
"""
import sys, math, argparse, io, contextlib
sys.path.insert(0, '.')
import backtest as bt


def run_spread(years=4.0, long_delta=None, protect_ratio=1.0, circuit_breaker=True,
               always_one=True, label=""):
    """long_delta=None → put nu (référence). Sinon jambe longue au delta cible."""
    days = bt.fetch_history(years + 0.15)
    closes_hist, dvol_30, dvol_hist = [], [], []
    positions = []   # {k_s, k_l, tte_left, contracts, net_prem_usd}
    hedge_qty = hedge_vwap = 0.0
    cash = 0.0
    equity_curve, daily_pnls = [], []
    n_trades = n_exp_itm = 0
    long_cost_total = 0.0
    risk_off = False
    n_cb = 0

    HC = bt.BA_HAIRCUT_VOLPTS

    for day in days:
        S, dvol = day['spot'], day['dvol']
        closes_hist.append(S); dvol_30.append(dvol); dvol_30 = dvol_30[-30:]
        dvol_hist.append(dvol)
        hv10, hv30 = bt.hv_from(closes_hist, 10), bt.hv_from(closes_hist, 30)
        if hv10 is None or hv30 is None or len(dvol_30) < 10:
            continue
        hv5 = bt.hv_from(closes_hist, 5)
        hv_blend = 0.5*hv10 + 0.5*hv30
        iv_rank  = max(0.0, min(1.0, (dvol-min(dvol_30))/max(max(dvol_30)-min(dvol_30), 5)))
        move_3d  = abs(S/closes_hist[-4]-1)*100 if len(closes_hist) >= 4 else 0.0
        day_pnl0 = cash  # snapshot pour pnl du jour (réalisé) — on suit l'équity à la place

        # helper de prix de put avec une IV donnée (en %), skew inclus
        def put_at(K, T, iv_shift):
            otm = (S-K)/S*100
            iv = dvol*(1+bt.SKEW_SLOPE*max(otm,0)) + iv_shift
            return bt.bs_put(S, K, T, iv/100)

        # ── 0. Circuit breaker ──
        if circuit_breaker:
            mv3s = (S/closes_hist[-4]-1)*100 if len(closes_hist) >= 4 else 0.0
            dch3 = dvol - dvol_hist[-4] if len(dvol_hist) >= 4 else 0.0
            if not risk_off and positions and (mv3s < -bt.CB_MOVE_3D_PCT or dch3 > bt.CB_DVOL_3D_PTS):
                for p in positions:
                    T = p['tte_left']/365
                    ps,_,_ = put_at(p['k_s'], T, +HC)        # rachète court à l'ask
                    pl = 0.0
                    if p['k_l']:
                        pl,_,_ = put_at(p['k_l'], T, -HC)    # revend long au bid
                    cash += p['net_prem_usd'] - (ps - pl)*p['contracts']
                positions = []
                if hedge_qty != 0:
                    cash += hedge_qty*(hedge_vwap-S); hedge_qty = hedge_vwap = 0.0
                risk_off = True; n_cb += 1
            elif risk_off:
                if hv5 is not None and hv5 < hv10 and move_3d < bt.CB_REENTRY_MOVE:
                    risk_off = False

        # ── 1. Expiration ──
        still = []
        for p in positions:
            p['tte_left'] -= 1
            if p['tte_left'] <= 0:
                payoff = max(p['k_s']-S, 0.0)
                if p['k_l']:
                    payoff -= max(p['k_l']-S, 0.0)
                payoff *= p['contracts']
                cash += p['net_prem_usd'] - payoff
                if payoff > 0: n_exp_itm += 1
            else:
                still.append(p)
        positions = still

        # ── 2. Mark-to-model + delta net ──
        net_delta = mtm_value = 0.0
        for p in positions:
            T = p['tte_left']/365
            ps, ds, gs = put_at(p['k_s'], T, 0.0)
            dl = gl = pl = 0.0
            if p['k_l']:
                pl, dl, gl = put_at(p['k_l'], T, 0.0)
            net_delta += (ds - dl) * p['contracts']     # convention long-option (cf docstring)
            mtm_value += (ps - pl) * p['contracts']      # coût de rachat du spread

        # ── 3. Hedge ──
        target = -net_delta; drift = abs(target-hedge_qty)
        thr = max(0.03, min(0.08, 0.05*60/max(dvol, 20)))
        if drift > thr*max(sum(p['contracts'] for p in positions), 1):
            dq = target - hedge_qty
            if hedge_qty != 0 and dq*hedge_qty < 0:
                closed = min(abs(dq), abs(hedge_qty))*(1 if hedge_qty > 0 else -1)
                cash += closed*(hedge_vwap-S)
            if target != 0:
                if hedge_qty*target > 0 and abs(target) > abs(hedge_qty):
                    add = target-hedge_qty
                    hedge_vwap = (hedge_vwap*abs(hedge_qty)+S*abs(add))/abs(target)
                elif hedge_qty*target <= 0:
                    hedge_vwap = S
            hedge_qty = target
        cash -= abs(hedge_qty)*S*bt.FUNDING_DAILY

        # ── 4. Entrée (sélection jambe courte identique à backtest.py) ──
        used = sum(p['contracts'] for p in positions)
        must_open = always_one and not positions and not risk_off
        if not risk_off and ((dvol >= bt.DVOL_MIN and used < bt.MAX_PORTFOLIO_BTC) or must_open):
            best = None
            for tte in bt.TTE_CHOICES:
                T = tte/365
                for td in bt.DELTA_TARGETS:
                    K = bt.strike_for_delta(S, T, dvol/100, td)
                    otm = (S-K)/S*100
                    if otm < 2: continue
                    price, delta, gamma = put_at(K, T, -HC)   # bid
                    if price < bt.MIN_PREMIUM_USD: continue
                    yield_a = (price/S)/T
                    bid_iv  = dvol*(1+bt.SKEW_SLOPE*otm) - HC
                    s_ivhv  = max(0.0, min(1.0, bid_iv/hv_blend-1.0))
                    z       = (otm/100)/max(hv_blend/100*math.sqrt(T), 1e-9)
                    s_yield = min(1.0, yield_a*z/bt.YIELD_NORM)
                    skew    = bid_iv/dvol-1.0
                    s_skew  = max(0.0, min(1.0, skew/bt.SKEW_NORM))
                    g_pts   = gamma*S*0.01*100
                    g_fac   = max(0.0, 1.0-max(0.0, g_pts-bt.GAMMA_PEN_START)/(bt.GAMMA_SCORE_CAP-bt.GAMMA_PEN_START))
                    score   = (0.40*s_ivhv+0.30*s_yield+0.30*s_skew)*g_fac
                    if best is None or score > best['score']:
                        best = {'score': score, 'K': K, 'tte': tte, 'price': price, 'td': td}
            ok = best and (best['score'] >= bt.ENTRY_SCORE_MIN or must_open)
            if ok:
                size = round(best['score']**bt.SIZE_CONVEXITY * (0.5+0.5*iv_rank), 1)
                size = max(0.1, min(size, bt.MAX_PORTFOLIO_BTC - used))
                if size >= 0.1:
                    k_s = best['K']; T = best['tte']/365
                    short_bid = best['price']
                    k_l = None; long_ask = 0.0
                    if long_delta is not None:
                        K_l = bt.strike_for_delta(S, T, dvol/100, long_delta)
                        if K_l < k_s - 1e-6:   # jambe longue plus OTM que la courte
                            pl,_,_ = put_at(K_l, T, +HC)   # achetée à l'ask
                            k_l = K_l; long_ask = pl
                            long_cost_total += long_ask * size * protect_ratio
                    net_prem = (short_bid - long_ask*protect_ratio) * size
                    positions.append({'k_s': k_s, 'k_l': k_l, 'tte_left': best['tte'],
                                      'contracts': size, 'net_prem_usd': net_prem})
                    n_trades += 1

        # ── 5. Equity ──
        open_prem = sum(p['net_prem_usd'] for p in positions)
        hedge_mtm = hedge_qty*(hedge_vwap-S)
        equity = cash + open_prem - mtm_value + hedge_mtm
        eq_prev = equity_curve[-1][1] if equity_curve else equity
        equity_curve.append((day['date'], equity, S, dvol))
        daily_pnls.append((equity-eq_prev, day['date']))

    # ── Stats ──
    eq = [e[1] for e in equity_curve]
    rets = [eq[i]-eq[i-1] for i in range(1, len(eq))]
    peak, max_dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v); max_dd = max(max_dd, peak-v)
    m = sum(rets)/len(rets); s = (sum((r-m)**2 for r in rets)/len(rets))**0.5
    sharpe = m/s*math.sqrt(365) if s > 0 else 0
    pnl_an = eq[-1]/len(eq)*365
    calmar = pnl_an/max_dd if max_dd > 0 else 0
    worst_day = min(daily_pnls)
    return {'label': label, 'pnl': eq[-1], 'pnl_an': pnl_an, 'maxdd': max_dd,
            'sharpe': sharpe, 'calmar': calmar, 'trades': n_trades, 'itm': n_exp_itm,
            'ncb': n_cb, 'long_cost': long_cost_total, 'worst_day': worst_day[0],
            'worst_date': worst_day[1]}


CONFIGS = [
    # label, long_delta, protect_ratio, circuit_breaker
    ("Put nu + CB (PRODUCTION)",        None,  1.0, True),
    ("Put nu, sans CB",                 None,  1.0, False),
    ("Spread L-0.05 + CB",             -0.05,  1.0, True),
    ("Spread L-0.05, sans CB",         -0.05,  1.0, False),
    ("Spread L-0.08 + CB",             -0.08,  1.0, True),
    ("Spread L-0.08, sans CB",         -0.08,  1.0, False),
    ("Spread L-0.10, sans CB",         -0.10,  1.0, False),
    ("Spread L-0.05 ratio0.5 + CB",    -0.05,  0.5, True),
    ("Spread L-0.05 ratio0.5 sans CB", -0.05,  0.5, False),
]

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--years', type=float, default=4.0)
    a = ap.parse_args()

    print(f"\n  PUT SPREAD vs PUT NU — BTC, convexité 1.5, {a.years} ans")
    print(f"  {'Config':<32} {'PnL':>9} {'MaxDD':>8} {'Calmar':>7} {'Sharpe':>7} "
          f"{'PireJour':>9} {'ITM':>4} {'CoûtLong':>9}")
    print(f"  {'-'*32} {'-'*9} {'-'*8} {'-'*7} {'-'*7} {'-'*9} {'-'*4} {'-'*9}")
    rows = []
    for label, ld, pr, cb in CONFIGS:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = run_spread(a.years, long_delta=ld, protect_ratio=pr, circuit_breaker=cb, label=label)
        rows.append(r)
        print(f"  {label:<32} {r['pnl']:>8,.0f}$ {r['maxdd']:>7,.0f}$ "
              f"{r['calmar']:>7.2f} {r['sharpe']:>7.2f} {r['worst_day']:>8,.0f}$ "
              f"{r['itm']:>4} {r['long_cost']:>8,.0f}$")
    print(f"\n  PireJour = pire perte sur une seule journée (ce que le spread doit plafonner)")
    print(f"  CoûtLong = prime totale payée sur les jambes longues (le coût de l'assurance)")
    best = max(rows, key=lambda r: r['calmar'])
    print(f"\n  >> Meilleur Calmar : {best['label']}  "
          f"(PnL {best['pnl']:,.0f}$, MaxDD {best['maxdd']:,.0f}$, pire jour {best['worst_day']:,.0f}$)")
