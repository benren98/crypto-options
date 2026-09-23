"""
backtest_tactical.py — Hedge gamma TACTIQUE : on n'achète des puts protecteurs
QUE lorsqu'on entre dans une « zone de danger » (stress qui monte vers le CB),
et on les revend dès que le danger passe. Le circuit breaker reste actif derrière.

Idée : éviter le bleed theta permanent du put spread (cf backtest_putspread.py,
40k$ de prime payée pour rien). Ici la protection ne coûte que pendant les
épisodes de stress (quelques jours par an), pour cushionner l'écart entre le
début du stress et le déclenchement du CB.

Zone de danger (seuils < CB) :
    move_3d_signed < −DANGER_MOVE   (CB = −10%)
    OU dvol_chg_3d > DANGER_DVOL    (CB = +12 pts)
    [option : exiger aussi hv5 > hv10, i.e. réalisé court qui accélère]

Deux books marqués au mark-to-market :
    - shorts VRP (vendus, prime encaissée)
    - hedge longs (puts achetés à l'ask, revendus au bid)

Usage : python backtest_tactical.py [--years 4]
"""
import sys, math, argparse, io, contextlib
sys.path.insert(0, '.')
import backtest as bt

HC = bt.BA_HAIRCUT_VOLPTS


def run_tac(years=4.0, danger_move=None, danger_dvol=None, hedge_delta=-0.10,
            hedge_tte=7, hedge_ratio=1.0, require_accel=False,
            circuit_breaker=True, always_one=True, label=""):
    """danger_move=None → pas d'overlay tactique (référence put nu + CB)."""
    days = bt.fetch_history(years + 0.15)
    closes_hist, dvol_30, dvol_hist = [], [], []
    shorts = []       # {k, tte_left, contracts, net_prem_usd}
    hedges = []       # {k, tte_left, contracts, entry_cost_usd}
    hedge_qty = hedge_vwap = 0.0
    cash = 0.0
    equity_curve, daily_pnls = [], []
    n_trades = n_exp_itm = 0
    n_hedge_buys = 0
    hedge_cost_total = hedge_payoff_total = 0.0
    risk_off = False
    n_cb = 0
    days_hedged = 0

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

        def put_at(K, T, iv_shift):
            otm = (S-K)/S*100
            iv = dvol*(1+bt.SKEW_SLOPE*max(otm,0)) + iv_shift
            return bt.bs_put(S, K, T, iv/100)

        def close_all_hedges(reason_sell=True):
            nonlocal cash, hedges, hedge_payoff_total
            for h in hedges:
                pl,_,_ = put_at(h['k'], h['tte_left']/365, -HC)   # revend au bid
                cash += pl*h['contracts'] - h['entry_cost_usd']
                hedge_payoff_total += pl*h['contracts']
            hedges = []

        # ── 0. Circuit breaker (ferme shorts ET hedges) ──
        if circuit_breaker:
            if not risk_off and shorts and (mv3s < -bt.CB_MOVE_3D_PCT or dch3 > bt.CB_DVOL_3D_PTS):
                for p in shorts:
                    ps,_,_ = put_at(p['k'], p['tte_left']/365, +HC)
                    cash += p['net_prem_usd'] - ps*p['contracts']
                shorts = []
                close_all_hedges()
                if hedge_qty != 0:
                    cash += hedge_qty*(hedge_vwap-S); hedge_qty = hedge_vwap = 0.0
                risk_off = True; n_cb += 1
            elif risk_off:
                if hv5 is not None and hv5 < hv10 and move_3d < bt.CB_REENTRY_MOVE:
                    risk_off = False

        # ── 1. Expiration des shorts ──
        still = []
        for p in shorts:
            p['tte_left'] -= 1
            if p['tte_left'] <= 0:
                payoff = max(p['k']-S, 0.0)*p['contracts']
                cash += p['net_prem_usd'] - payoff
                if payoff > 0: n_exp_itm += 1
            else:
                still.append(p)
        shorts = still
        # ── 1b. Expiration des hedges (on encaisse le payoff) ──
        still_h = []
        for h in hedges:
            h['tte_left'] -= 1
            if h['tte_left'] <= 0:
                payoff = max(h['k']-S, 0.0)*h['contracts']
                cash += payoff - h['entry_cost_usd']
                hedge_payoff_total += payoff
            else:
                still_h.append(h)
        hedges = still_h

        # ── 2. Overlay tactique : entrer/sortir du hedge selon le danger ──
        if danger_move is not None and not risk_off:
            danger = (mv3s < -danger_move) or (dch3 > danger_dvol)
            if require_accel:
                danger = danger and (hv5 is not None and hv5 > hv10)
            used = sum(p['contracts'] for p in shorts)
            if danger and not hedges and used > 0:
                n_h = round(used * hedge_ratio, 1)
                if n_h >= 0.1:
                    Th = hedge_tte/365
                    K_h = bt.strike_for_delta(S, Th, dvol/100, hedge_delta)
                    pl,_,_ = put_at(K_h, Th, +HC)   # achète à l'ask
                    hedges.append({'k': K_h, 'tte_left': hedge_tte, 'contracts': n_h,
                                   'entry_cost_usd': pl*n_h})
                    hedge_cost_total += pl*n_h
                    n_hedge_buys += 1
            elif (not danger) and hedges:
                close_all_hedges()
            if hedges:
                days_hedged += 1

        # ── 3. Mark-to-model + delta net (shorts + hedges) ──
        net_delta = 0.0
        open_short_pnl = open_hedge_pnl = 0.0
        for p in shorts:
            ps, ds, _ = put_at(p['k'], p['tte_left']/365, 0.0)
            net_delta += ds*p['contracts']
            open_short_pnl += p['net_prem_usd'] - ps*p['contracts']
        for h in hedges:
            pl, dl, _ = put_at(h['k'], h['tte_left']/365, 0.0)
            net_delta += -dl*h['contracts']     # long put : signe opposé (cf dérivation)
            open_hedge_pnl += pl*h['contracts'] - h['entry_cost_usd']

        # ── 4. Hedge perp ──
        target = -net_delta; drift = abs(target-hedge_qty)
        thr = max(0.03, min(0.08, 0.05*60/max(dvol, 20)))
        ncon = max(sum(p['contracts'] for p in shorts) + sum(h['contracts'] for h in hedges), 1)
        if drift > thr*ncon:
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

        # ── 5. Entrée short (sélection identique à backtest.py) ──
        used = sum(p['contracts'] for p in shorts)
        must_open = always_one and not shorts and not risk_off
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
                    score   = (0.40*s_ivhv+0.30*s_yield+0.30*s_skew)*g_fac
                    if best is None or score > best['score']:
                        best = {'score': score, 'K': K, 'tte': tte, 'price': price}
            ok = best and (best['score'] >= bt.ENTRY_SCORE_MIN or must_open)
            if ok:
                size = round(best['score']**bt.SIZE_CONVEXITY * (0.5+0.5*iv_rank), 1)
                size = max(0.1, min(size, bt.MAX_PORTFOLIO_BTC - used))
                if size >= 0.1:
                    shorts.append({'k': best['K'], 'tte_left': best['tte'],
                                   'contracts': size, 'net_prem_usd': best['price']*size})
                    n_trades += 1

        # ── 6. Equity ──
        equity = cash + open_short_pnl + open_hedge_pnl + hedge_qty*(hedge_vwap-S)
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
    worst_day, worst_date = min(daily_pnls)
    return {'label': label, 'pnl': eq[-1], 'maxdd': max_dd, 'sharpe': sharpe,
            'calmar': calmar, 'itm': n_exp_itm, 'worst_day': worst_day,
            'n_buys': n_hedge_buys, 'days_hedged': days_hedged,
            'hedge_cost': hedge_cost_total, 'hedge_payoff': hedge_payoff_total,
            'net_hedge': hedge_payoff_total - hedge_cost_total}


CONFIGS = [
    # label, danger_move, danger_dvol, hedge_delta, hedge_tte, ratio, accel
    ("Référence (put nu + CB)",          None, None,  -0.10,  7, 1.0, False),
    ("Tac −5%/+6, L-.10, 7j, r1",         5.0,  6.0,  -0.10,  7, 1.0, False),
    ("Tac −6%/+7, L-.10, 7j, r1",         6.0,  7.0,  -0.10,  7, 1.0, False),
    ("Tac −5%/+6, L-.15, 7j, r1",         5.0,  6.0,  -0.15,  7, 1.0, False),
    ("Tac −5%/+6, L-.10, 14j, r1",        5.0,  6.0,  -0.10, 14, 1.0, False),
    ("Tac −5%/+6, L-.10, 7j, r1.5",       5.0,  6.0,  -0.10,  7, 1.5, False),
    ("Tac −5%/+6, L-.10, 7j, r1, accel",  5.0,  6.0,  -0.10,  7, 1.0, True),
    ("Tac −4%/+5, L-.10, 7j, r1",         4.0,  5.0,  -0.10,  7, 1.0, False),
    ("Tac −7%/+8, L-.15, 10j, r1.5",      7.0,  8.0,  -0.15, 10, 1.5, False),
]

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--years', type=float, default=4.0)
    a = ap.parse_args()

    print(f"\n  HEDGE TACTIQUE (protection seulement en zone de danger) — BTC, {a.years} ans")
    print(f"  {'Config':<34} {'PnL':>9} {'MaxDD':>8} {'Calmar':>7} {'PireJ':>8} "
          f"{'achats':>7} {'jHedge':>7} {'netHedge':>9}")
    print(f"  {'-'*34} {'-'*9} {'-'*8} {'-'*7} {'-'*8} {'-'*7} {'-'*7} {'-'*9}")
    rows = []
    for (label, dm, dv, hd, ht, hr, ac) in CONFIGS:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = run_tac(a.years, danger_move=dm, danger_dvol=dv, hedge_delta=hd,
                        hedge_tte=ht, hedge_ratio=hr, require_accel=ac, label=label)
        rows.append(r)
        print(f"  {label:<34} {r['pnl']:>8,.0f}$ {r['maxdd']:>7,.0f}$ "
              f"{r['calmar']:>7.2f} {r['worst_day']:>7,.0f}$ {r['n_buys']:>7} "
              f"{r['days_hedged']:>7} {r['net_hedge']:>8,.0f}$")
    print(f"\n  PireJ    = pire perte sur une journée (ce que le hedge doit cushionner)")
    print(f"  netHedge = payoff total des hedges − prime payée (>0 = l'assurance a rapporté)")
    ref = rows[0]
    best = max(rows[1:], key=lambda r: r['calmar'])
    print(f"\n  Référence : PnL {ref['pnl']:,.0f}$, MaxDD {ref['maxdd']:,.0f}$, Calmar {ref['calmar']:.2f}")
    print(f"  Meilleur tactique : {best['label']}  "
          f"PnL {best['pnl']:,.0f}$, MaxDD {best['maxdd']:,.0f}$, Calmar {best['calmar']:.2f}")
