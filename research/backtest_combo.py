"""
backtest_combo.py — Empilage des leviers gagnants (BTC).

Moteur complet avec tous les leviers activables :
    - poids de score (skew+ : .30/.25/.45)            [C1 — gratuit en PnL]
    - sélectivité (entry_min) + relâchement always_one [C2]
    - circuit breaker gradué (−5/+6 keep.3 rest3)     [A2 — −11% PnL]

But : trouver l'empilage qui minimise le MaxDD en gardant le PnL ~neutre.

Usage : python backtest_combo.py [--years 4]
"""
import sys, math, argparse, io, contextlib
sys.path.insert(0, '.')
import backtest as bt
import backtest_eth as be

HC = bt.BA_HAIRCUT_VOLPTS


def run_combo(years=4.0, w=(0.40,0.30,0.30), entry_min=0.45, always_one=True,
              grad=False, keep=0.3, t1_restore=3.0, trig=None, days=None):
    """trig : dict de seuils pour le palier d'allègement (OR de tous ceux fournis) :
        move1/move2/move3 : |chute spot| Nj signée < −seuil
        dvol1/dvol3       : hausse DVOL Nj > +seuil
        hv5x              : HV5 > hv5x × HV10 (réalisé court qui accélère)
       Défaut (None) = move3=5.0, dvol3=6.0 (équivalent A2 d'origine)."""
    if trig is None:
        trig = {'move3': 5.0, 'dvol3': 6.0}
    if days is None:
        days = bt.fetch_history(years + 0.15)
    w_ivhv, w_yield, w_skew = w
    closes_hist, dvol_30, dvol_hist = [], [], []
    positions = []
    hedge_qty = hedge_vwap = 0.0
    cash = 0.0
    equity_curve, daily_pnls = [], []
    entries   = []   # (TTE jours, prime $/BTC, size BTC) de chaque entrée
    n_exp_itm = 0
    state = 0   # 0 normal, 1 allégé, 2 fermé

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
        mv2s     = (S/closes_hist[-3]-1)*100 if len(closes_hist) >= 3 else 0.0
        mv1s     = (S/closes_hist[-2]-1)*100 if len(closes_hist) >= 2 else 0.0
        dch3     = dvol - dvol_hist[-4] if len(dvol_hist) >= 4 else 0.0
        dch1     = dvol - dvol_hist[-2] if len(dvol_hist) >= 2 else 0.0

        def tier1_trigger():
            if 'move1' in trig and mv1s < -trig['move1']: return True
            if 'move2' in trig and mv2s < -trig['move2']: return True
            if 'move3' in trig and mv3s < -trig['move3']: return True
            if 'dvol1' in trig and dch1 > trig['dvol1']:  return True
            if 'dvol3' in trig and dch3 > trig['dvol3']:  return True
            if 'hv5x'  in trig and hv5 is not None and hv5 > trig['hv5x']*hv10: return True
            return False

        def put_at(K, T, sh):
            otm = (S-K)/S*100
            iv = dvol*(1+bt.SKEW_SLOPE*max(otm,0)) + sh
            return bt.bs_put(S, K, T, iv/100)

        # circuit breaker (binaire ou gradué)
        t2_hit = mv3s < -bt.CB_MOVE_3D_PCT or dch3 > bt.CB_DVOL_3D_PTS
        reentry_ok = (hv5 is not None and hv5 < hv10 and move_3d < bt.CB_REENTRY_MOVE)
        if state < 2 and positions and t2_hit:
            for p in positions:
                ps,_,_ = put_at(p['k'], p['tte_left']/365, +HC)
                cash += p['net_prem_usd'] - ps*p['contracts']
            positions = []
            if hedge_qty != 0:
                cash += hedge_qty*(hedge_vwap-S); hedge_qty = hedge_vwap = 0.0
            state = 2
        elif grad and state == 0 and positions and tier1_trigger():
            for p in positions:
                sell = p['contracts']*(1.0-keep)
                ps,_,_ = put_at(p['k'], p['tte_left']/365, +HC)
                cash += p['net_prem_usd']*(1.0-keep) - ps*sell
                p['contracts'] *= keep; p['net_prem_usd'] *= keep
            state = 1
        elif grad and state == 1 and move_3d < t1_restore:
            state = 0
        elif state >= 1 and reentry_ok:
            state = 0

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

        eff_cap = bt.MAX_PORTFOLIO_BTC * (keep if (grad and state == 1) else 1.0)
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
                    score   = (w_ivhv*s_ivhv + w_yield*s_yield + w_skew*s_skew)*g_fac
                    if best is None or score > best['score']:
                        best = {'score': score, 'K': K, 'tte': tte, 'price': price}
            ok = best and (best['score'] >= entry_min or must_open)
            if ok:
                size = round(best['score']**bt.SIZE_CONVEXITY * (0.5+0.5*iv_rank), 1)
                size = max(0.1, min(size, eff_cap - used))
                if size >= 0.1:
                    positions.append({'k': best['K'], 'tte_left': best['tte'],
                                      'contracts': size, 'net_prem_usd': best['price']*size})
                    entries.append((best['tte'], best['price'], size))   # (TTE j, prime $/BTC, size)

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
    return {'pnl': eq[-1], 'maxdd': max_dd, 'sharpe': sharpe, 'calmar': calmar,
            'worst': worst, 'entries': entries}


SKEW_W = (0.30, 0.25, 0.45)
CONFIGS = [
    ("0. BASELINE production",          dict()),
    ("C1 skew+",                        dict(w=SKEW_W)),
    ("C1+C2 skew+ min.50 noAlways",     dict(w=SKEW_W, entry_min=0.50, always_one=False)),
    ("A2 CB gradué seul",               dict(grad=True)),
    ("A2 + C1 skew+",                   dict(w=SKEW_W, grad=True)),
    ("A2 + C1 + C2 (TOUT)",             dict(w=SKEW_W, entry_min=0.50, always_one=False, grad=True)),
]

if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('--years', type=float, default=4.0)
    a = ap.parse_args()
    print(f"\n  EMPILAGE DES LEVIERS — BTC, {a.years} ans")
    print(f"  {'Config':<32} {'PnL':>9} {'MaxDD':>8} {'Calmar':>7} {'Sharpe':>7} {'PireJ':>8}")
    print(f"  {'-'*32} {'-'*9} {'-'*8} {'-'*7} {'-'*7} {'-'*8}")
    rows = []
    for label, over in CONFIGS:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = run_combo(a.years, **over)
        rows.append((label, r))
        print(f"  {label:<32} {r['pnl']:>8,.0f}$ {r['maxdd']:>7,.0f}$ "
              f"{r['calmar']:>7.2f} {r['sharpe']:>7.2f} {r['worst']:>7,.0f}$")
    base = rows[0][1]
    print(f"\n  Baseline : PnL {base['pnl']:,.0f}$ · MaxDD {base['maxdd']:,.0f}$ · Calmar {base['calmar']:.2f}")
    best = max(rows[1:], key=lambda x: x[1]['calmar'])
    print(f"  Meilleur Calmar : {best[0]} · PnL {best[1]['pnl']:,.0f}$ · "
          f"MaxDD {best[1]['maxdd']:,.0f}$ · Calmar {best[1]['calmar']:.2f}")

    # Robustesse ETH du meilleur empilage gratuit (C1+C2) et du tout
    eth_raw = be.fetch_history(a.years + 0.15)
    eth_days = [{'date': d['date'], 'spot': d['spot'], 'dvol': d['dvol']} for d in eth_raw]
    print(f"\n  ── Robustesse ETH ──")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        e_base = run_combo(a.years, days=eth_days)
        e_c12  = run_combo(a.years, w=SKEW_W, entry_min=0.50, always_one=False, days=eth_days)
        e_all  = run_combo(a.years, w=SKEW_W, entry_min=0.50, always_one=False, grad=True, days=eth_days)
    for lab, r in [("ETH baseline", e_base), ("ETH C1+C2", e_c12), ("ETH TOUT", e_all)]:
        print(f"  {lab:<32} PnL {r['pnl']:>7,.0f}$ · MaxDD {r['maxdd']:>6,.0f}$ · Calmar {r['calmar']:>5.2f}")
