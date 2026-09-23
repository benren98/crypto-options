"""
backtest_scoring.py — Track C : calibration du scoring.

C2 : sélectivité d'entrée (ENTRY_SCORE_MIN) + relâchement de la règle
     « toujours ≥1 position » (autoriser le book à plat les jours faibles).
C1 : repondération des composantes (IV/HV, yield, skew).

Engine répliqué avec poids/seuil/always_one injectables (CB binaire, conv1.5).
On teste sur le baseline ; les gagnants seront ensuite combinés avec le CB gradué.

Usage : python backtest_scoring.py [--years 4]
"""
import sys, math, argparse, io, contextlib
sys.path.insert(0, '.')
import backtest as bt

HC = bt.BA_HAIRCUT_VOLPTS


def run_score(years=4.0, w_ivhv=0.40, w_yield=0.30, w_skew=0.30,
              entry_min=0.45, always_one=True, circuit_breaker=True):
    days = bt.fetch_history(years + 0.15)
    closes_hist, dvol_30, dvol_hist = [], [], []
    positions = []
    hedge_qty = hedge_vwap = 0.0
    cash = 0.0
    equity_curve, daily_pnls, notionals = [], [], []
    n_trades = n_exp_itm = flat_days = 0
    risk_off = False

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

        def put_at(K, T, sh):
            otm = (S-K)/S*100
            iv = dvol*(1+bt.SKEW_SLOPE*max(otm,0)) + sh
            return bt.bs_put(S, K, T, iv/100)

        if circuit_breaker:
            mv3s = (S/closes_hist[-4]-1)*100 if len(closes_hist) >= 4 else 0.0
            dch3 = dvol - dvol_hist[-4] if len(dvol_hist) >= 4 else 0.0
            if not risk_off and positions and (mv3s < -bt.CB_MOVE_3D_PCT or dch3 > bt.CB_DVOL_3D_PTS):
                for p in positions:
                    ps,_,_ = put_at(p['k'], p['tte_left']/365, +HC)
                    cash += p['net_prem_usd'] - ps*p['contracts']
                positions = []
                if hedge_qty != 0:
                    cash += hedge_qty*(hedge_vwap-S); hedge_qty = hedge_vwap = 0.0
                risk_off = True
            elif risk_off:
                if hv5 is not None and hv5 < hv10 and move_3d < bt.CB_REENTRY_MOVE:
                    risk_off = False

        still = []
        for p in positions:
            p['tte_left'] -= 1
            if p['tte_left'] <= 0:
                payoff = max(p['k']-S, 0.0)*p['contracts']
                cash += p['net_prem_usd'] - payoff
                if payoff > 0: n_exp_itm += 1
            else:
                still.append(p)
        positions = still

        net_delta = mtm = 0.0
        for p in positions:
            ps, ds, _ = put_at(p['k'], p['tte_left']/365, 0.0)
            net_delta += ds*p['contracts']; mtm += ps*p['contracts']
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
                    price, delta, gamma = put_at(K, T, -HC)
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
                    score   = (w_ivhv*s_ivhv + w_yield*s_yield + w_skew*s_skew)*g_fac
                    if best is None or score > best['score']:
                        best = {'score': score, 'K': K, 'tte': tte, 'price': price}
            ok = best and (best['score'] >= entry_min or must_open)
            if ok:
                size = round(best['score']**bt.SIZE_CONVEXITY * (0.5+0.5*iv_rank), 1)
                size = max(0.1, min(size, bt.MAX_PORTFOLIO_BTC - used))
                if size >= 0.1:
                    positions.append({'k': best['K'], 'tte_left': best['tte'],
                                      'contracts': size, 'net_prem_usd': best['price']*size})
                    n_trades += 1
        if not positions: flat_days += 1

        open_prem = sum(p['net_prem_usd'] for p in positions)
        equity = cash + open_prem - mtm + hedge_qty*(hedge_vwap-S)
        eq_prev = equity_curve[-1][1] if equity_curve else equity
        equity_curve.append((day['date'], equity, S, dvol))
        daily_pnls.append((equity-eq_prev, day['date']))
        notionals.append(sum(p['contracts'] for p in positions))

    eq = [e[1] for e in equity_curve]
    rets = [eq[i]-eq[i-1] for i in range(1, len(eq))]
    peak, max_dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v); max_dd = max(max_dd, peak-v)
    m = sum(rets)/len(rets); s = (sum((r-m)**2 for r in rets)/len(rets))**0.5
    sharpe = m/s*math.sqrt(365) if s > 0 else 0
    pnl_an = eq[-1]/len(eq)*365
    calmar = pnl_an/max_dd if max_dd > 0 else 0
    worst, _ = min(daily_pnls)
    return {'pnl': eq[-1], 'maxdd': max_dd, 'sharpe': sharpe, 'calmar': calmar,
            'worst': worst, 'flat': flat_days, 'avg_not': sum(notionals)/len(notionals)}


CONFIGS = [
    ("BASELINE (.40/.30/.30, min.45, always)", dict()),
    # C2 — sélectivité d'entrée + relâchement always_one
    ("min .50",                    dict(entry_min=0.50)),
    ("min .55",                    dict(entry_min=0.55)),
    ("min .45, NO always_one",     dict(always_one=False)),
    ("min .50, NO always_one",     dict(entry_min=0.50, always_one=False)),
    ("min .55, NO always_one",     dict(entry_min=0.55, always_one=False)),
    ("min .60, NO always_one",     dict(entry_min=0.60, always_one=False)),
    # C1 — repondération
    ("skew+ (.30/.25/.45)",        dict(w_ivhv=0.30, w_yield=0.25, w_skew=0.45)),
    ("ivhv+ (.55/.20/.25)",        dict(w_ivhv=0.55, w_yield=0.20, w_skew=0.25)),
    ("yield+ (.30/.45/.25)",       dict(w_ivhv=0.30, w_yield=0.45, w_skew=0.25)),
    ("skew+ & min.50 noAlways",    dict(w_ivhv=0.30, w_yield=0.25, w_skew=0.45, entry_min=0.50, always_one=False)),
]

if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('--years', type=float, default=4.0)
    a = ap.parse_args()
    print(f"\n  TRACK C — CALIBRATION SCORING — BTC, conv1.5 + CB binaire, {a.years} ans")
    print(f"  {'Config':<40} {'PnL':>9} {'MaxDD':>8} {'Calmar':>7} {'Sharpe':>7} {'PireJ':>8} {'flat':>5}")
    print(f"  {'-'*40} {'-'*9} {'-'*8} {'-'*7} {'-'*7} {'-'*8} {'-'*5}")
    rows = []
    for label, over in CONFIGS:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = run_score(a.years, **over)
        rows.append((label, r))
        print(f"  {label:<40} {r['pnl']:>8,.0f}$ {r['maxdd']:>7,.0f}$ "
              f"{r['calmar']:>7.2f} {r['sharpe']:>7.2f} {r['worst']:>7,.0f}$ {r['flat']:>5}")
    base = rows[0][1]; best = max(rows[1:], key=lambda x: x[1]['calmar'])
    print(f"\n  flat = nb de jours sans position (book à plat)")
    print(f"\n  Baseline : PnL {base['pnl']:,.0f}$ · MaxDD {base['maxdd']:,.0f}$ · Calmar {base['calmar']:.2f}")
    print(f"  Meilleur : {best[0]} · PnL {best[1]['pnl']:,.0f}$ · MaxDD {best[1]['maxdd']:,.0f}$ · Calmar {best[1]['calmar']:.2f}")
