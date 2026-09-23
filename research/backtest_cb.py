"""
backtest_cb.py — A2 : circuit breaker GRADUÉ (BTC).

Le CB actuel est binaire (tout ou rien) : il ferme tout à −10%/3j ou DVOL +12pts.
Hypothèse : un dé-risquage par paliers cushionne le gap avec moins de whipsaw.

États :
    0 normal   → pleine taille
    1 allégé   → seuil mou franchi : on rachète une fraction (1−keep) du book,
                 et le cap d'entrée est ramené à keep × MAX_PORTFOLIO_BTC
    2 fermé    → seuil dur franchi : on ferme tout + hedge à plat (CB actuel)

Re-entrée (stress retombé : HV5<HV10 et |move3j|<4%) : retour direct à l'état 0.

Référence = CB binaire actuel (tier1 désactivé).

Usage : python backtest_cb.py [--years 4]
"""
import sys, math, argparse, io, contextlib
sys.path.insert(0, '.')
import backtest as bt

HC = bt.BA_HAIRCUT_VOLPTS


def run_cb(years=4.0, t1_move=None, t1_dvol=None, keep=0.5,
           t2_move=10.0, t2_dvol=12.0, t1_restore=None, always_one=True, days=None):
    """t1_move=None → CB binaire (pas de palier intermédiaire).
    t1_restore : si fourni, l'état 1 (allégé) reprend dès que |move3j| < t1_restore
    (reprise rapide, sans exiger HV5<HV10). L'état 2 garde la re-entrée stricte.
    days : historique injectable (ex. ETH) ; sinon fetch BTC."""
    if days is None:
        days = bt.fetch_history(years + 0.15)
    closes_hist, dvol_30, dvol_hist = [], [], []
    positions = []
    hedge_qty = hedge_vwap = 0.0
    cash = 0.0
    equity_curve, daily_pnls = [], []
    n_trades = n_exp_itm = 0
    state = 0
    n_t1 = n_t2 = 0

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
        mv3s     = (S/closes_hist[-4]-1)*100 if len(closes_hist) >= 4 else 0.0
        dch3     = dvol - dvol_hist[-4] if len(dvol_hist) >= 4 else 0.0

        def put_at(K, T, sh):
            otm = (S-K)/S*100
            iv = dvol*(1+bt.SKEW_SLOPE*max(otm,0)) + sh
            return bt.bs_put(S, K, T, iv/100)

        t2_hit = mv3s < -t2_move or dch3 > t2_dvol
        t1_hit = (t1_move is not None) and (mv3s < -t1_move or dch3 > t1_dvol)
        reentry_ok = (hv5 is not None and hv5 < hv10 and move_3d < bt.CB_REENTRY_MOVE)

        # ── Circuit breaker gradué ──
        if state < 2 and positions and t2_hit:           # seuil dur → fermeture totale
            for p in positions:
                ps,_,_ = put_at(p['k'], p['tte_left']/365, +HC)
                cash += p['net_prem_usd'] - ps*p['contracts']
            positions = []
            if hedge_qty != 0:
                cash += hedge_qty*(hedge_vwap-S); hedge_qty = hedge_vwap = 0.0
            state = 2; n_t2 += 1
        elif state == 0 and positions and t1_hit:         # seuil mou → allègement
            for p in positions:
                sell = p['contracts'] * (1.0 - keep)
                ps,_,_ = put_at(p['k'], p['tte_left']/365, +HC)
                cash += p['net_prem_usd']*(1.0-keep) - ps*sell
                p['contracts'] *= keep
                p['net_prem_usd'] *= keep
            state = 1; n_t1 += 1
        elif state == 1 and t1_restore is not None and move_3d < t1_restore:
            state = 0                                      # reprise rapide depuis l'allègement
        elif state >= 1 and reentry_ok:                   # stress retombé → reprise (stricte)
            state = 0

        # ── Expiration ──
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

        # ── Mark + hedge ──
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

        # ── Entrée (cap réduit en état 1) ──
        eff_cap = bt.MAX_PORTFOLIO_BTC * (keep if state == 1 else 1.0)
        used = sum(p['contracts'] for p in positions)
        must_open = always_one and not positions and state == 0
        if state != 2 and ((dvol >= bt.DVOL_MIN and used < eff_cap) or must_open):
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
                    score   = (0.40*s_ivhv+0.30*s_yield+0.30*s_skew)*g_fac
                    if best is None or score > best['score']:
                        best = {'score': score, 'K': K, 'tte': tte, 'price': price}
            ok = best and (best['score'] >= bt.ENTRY_SCORE_MIN or must_open)
            if ok:
                size = round(best['score']**bt.SIZE_CONVEXITY * (0.5+0.5*iv_rank), 1)
                size = max(0.1, min(size, eff_cap - used))
                if size >= 0.1:
                    positions.append({'k': best['K'], 'tte_left': best['tte'],
                                      'contracts': size, 'net_prem_usd': best['price']*size})
                    n_trades += 1

        open_prem = sum(p['net_prem_usd'] for p in positions)
        equity = cash + open_prem - mtm + hedge_qty*(hedge_vwap-S)
        eq_prev = equity_curve[-1][1] if equity_curve else equity
        equity_curve.append((day['date'], equity, S, dvol))
        daily_pnls.append((equity-eq_prev, day['date']))

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
    # PnL par année civile (robustesse : le bénéfice est-il réparti ?)
    by_year, prev = {}, 0.0
    for (d, e, *_), in [(x,) for x in equity_curve]:
        by_year[d.year] = e
    annual, prev = {}, 0.0
    for y in sorted(by_year):
        annual[y] = by_year[y] - prev; prev = by_year[y]
    return {'pnl': eq[-1], 'maxdd': max_dd, 'sharpe': sharpe, 'calmar': calmar,
            'itm': n_exp_itm, 'worst': worst, 'n_t1': n_t1, 'n_t2': n_t2,
            'annual': annual}


CONFIGS = [
    # label, t1_move, t1_dvol, keep, t1_restore — confirmation autour du gagnant
    ("BASELINE CB binaire",        None, None, 0.5, None),
    ("−5/+6 keep.3 rest3 (WIN)",    5.0,  6.0, 0.3, 3.0),
    ("−5/+6 keep.2 rest3",          5.0,  6.0, 0.2, 3.0),
    ("−5/+6 keep.4 rest3",          5.0,  6.0, 0.4, 3.0),
    ("−5/+6 keep.3 rest2",          5.0,  6.0, 0.3, 2.0),
    ("−5/+6 keep.3 rest4",          5.0,  6.0, 0.3, 4.0),
    ("−4/+5 keep.3 rest3",          4.0,  5.0, 0.3, 3.0),
    ("−6/+7 keep.3 rest3",          6.0,  7.0, 0.3, 3.0),
    ("−5/+5 keep.3 rest3",          5.0,  5.0, 0.3, 3.0),
]

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--years', type=float, default=4.0)
    a = ap.parse_args()
    print(f"\n  A2 — CIRCUIT BREAKER GRADUÉ — BTC, conv1.5, {a.years} ans")
    print(f"  {'Config':<26} {'PnL':>9} {'MaxDD':>8} {'Calmar':>7} {'Sharpe':>7} {'PireJ':>8} {'allèg':>6} {'ferm':>5}")
    print(f"  {'-'*26} {'-'*9} {'-'*8} {'-'*7} {'-'*7} {'-'*8} {'-'*6} {'-'*5}")
    rows = []
    for (label, t1m, t1d, keep, rest) in CONFIGS:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = run_cb(a.years, t1_move=t1m, t1_dvol=t1d, keep=keep, t1_restore=rest)
        rows.append((label, r))
        print(f"  {label:<26} {r['pnl']:>8,.0f}$ {r['maxdd']:>7,.0f}$ "
              f"{r['calmar']:>7.2f} {r['sharpe']:>7.2f} {r['worst']:>7,.0f}$ "
              f"{r['n_t1']:>6} {r['n_t2']:>5}")
    base = rows[0][1]
    best = max(rows[1:], key=lambda x: x[1]['calmar'])
    print(f"\n  allèg = nb d'allègements (palier 1) · ferm = nb de fermetures totales (palier 2)")
    print(f"\n  Baseline : PnL {base['pnl']:,.0f}$ · MaxDD {base['maxdd']:,.0f}$ · Calmar {base['calmar']:.2f}")
    print(f"  Meilleur : {best[0]} · PnL {best[1]['pnl']:,.0f}$ · MaxDD {best[1]['maxdd']:,.0f}$ · Calmar {best[1]['calmar']:.2f}")
