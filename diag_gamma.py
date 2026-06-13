"""
diag_gamma.py — Diagnostic : le budget gamma mord-il vraiment ?

Rejoue le sizing BASELINE (actuel, sans budget gamma) et enregistre chaque jour :
    - le gamma dollar AGRÉGÉ du portefeuille (port_gpts, pts de delta / 1% move)
    - le gamma INDIVIDUEL à l'entrée de chaque nouvelle position
    - le PnL du jour (pour repérer la fenêtre de max drawdown)

Répond à : « dans le scénario de max drawdown, le portefeuille contient-il des
positions au gamma agrégé > 6 ? Et entre-t-on jamais sur une option à g_pts > 6 ? »
"""
import sys, math
sys.path.insert(0, '.')
import backtest as bt

def run_diag(years=4.0):
    days = bt.fetch_history(years + 0.15)
    closes_hist, dvol_30, dvol_hist = [], [], []
    positions = []
    hedge_qty = hedge_vwap = 0.0
    cash = 0.0
    risk_off = False
    daily = []           # (date, port_gpts, equity, day_pnl, n_pos, used_btc)
    entry_gammas = []    # g_pts individuel à chaque entrée

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

        # CB (aligné)
        mv3s = (S/closes_hist[-4]-1)*100 if len(closes_hist) >= 4 else 0.0
        dch3 = dvol - dvol_hist[-4] if len(dvol_hist) >= 4 else 0.0
        if not risk_off and positions and (mv3s < -bt.CB_MOVE_3D_PCT or dch3 > bt.CB_DVOL_3D_PTS):
            for p in positions:
                T = p['tte_left']/365; otm = (S-p['strike'])/S*100
                sig = (dvol*(1+bt.SKEW_SLOPE*max(otm,0))+bt.BA_HAIRCUT_VOLPTS)/100
                price,_,_ = bt.bs_put(S, p['strike'], T, sig)
                cash += p['entry_premium_usd'] - price*p['contracts']
            positions = []
            if hedge_qty != 0:
                cash += hedge_qty*(hedge_vwap-S); hedge_qty = hedge_vwap = 0.0
            risk_off = True
        elif risk_off:
            if hv5 is not None and hv5 < hv10 and move_3d < bt.CB_REENTRY_MOVE:
                risk_off = False

        # expiration
        still = []
        for p in positions:
            p['tte_left'] -= 1
            if p['tte_left'] <= 0:
                payoff = max(p['strike']-S, 0.0)*p['contracts']
                cash += p['entry_premium_usd'] - payoff
            else:
                still.append(p)
        positions = still

        # mark + gamma agrégé
        net_delta = mtm_value = port_gpts = 0.0
        for p in positions:
            T = p['tte_left']/365; otm = (S-p['strike'])/S*100
            sig = dvol/100*(1+bt.SKEW_SLOPE*max(otm,0))
            price, delta, gamma = bt.bs_put(S, p['strike'], T, sig)
            net_delta += delta*p['contracts']
            mtm_value += price*p['contracts']
            port_gpts += gamma*S*0.01*100 * p['contracts']

        # hedge
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

        # entrée BASELINE (size = score × (0.5+0.5 rank))
        used = sum(p['contracts'] for p in positions)
        must_open = not positions and not risk_off
        if not risk_off and ((dvol >= bt.DVOL_MIN and used < bt.MAX_PORTFOLIO_BTC) or must_open):
            best = None
            for tte in bt.TTE_CHOICES:
                T = tte/365
                for td in bt.DELTA_TARGETS:
                    K = bt.strike_for_delta(S, T, dvol/100, td)
                    otm = (S-K)/S*100
                    if otm < 2: continue
                    mark_iv = dvol*(1+bt.SKEW_SLOPE*otm); bid_iv = mark_iv-bt.BA_HAIRCUT_VOLPTS
                    price, delta, gamma = bt.bs_put(S, K, T, bid_iv/100)
                    if price < bt.MIN_PREMIUM_USD: continue
                    yield_a = (price/S)/T
                    s_ivhv  = max(0.0, min(1.0, bid_iv/hv_blend-1.0))
                    z       = (otm/100)/max(hv_blend/100*math.sqrt(T), 1e-9)
                    s_yield = min(1.0, yield_a*z/bt.YIELD_NORM)
                    skew    = bid_iv/dvol-1.0
                    s_skew  = max(0.0, min(1.0, skew/bt.SKEW_NORM))
                    g_pts   = gamma*S*0.01*100
                    g_fac   = max(0.0, 1.0-max(0.0, g_pts-bt.GAMMA_PEN_START)/(bt.GAMMA_SCORE_CAP-bt.GAMMA_PEN_START))
                    score   = (0.40*s_ivhv+0.30*s_yield+0.30*s_skew)*g_fac
                    if best is None or score > best['score']:
                        best = {'score': score, 'K': K, 'tte': tte, 'price': price, 'gpts': g_pts}
            ok = best and (best['score'] >= bt.ENTRY_SCORE_MIN or must_open)
            if ok:
                size = round(best['score']*(0.5+0.5*iv_rank), 1)
                size = max(0.1, min(size, bt.MAX_PORTFOLIO_BTC - used))
                if size >= 0.1:
                    positions.append({'strike': best['K'], 'tte_left': best['tte'],
                                      'contracts': size, 'entry_premium_usd': best['price']*size})
                    entry_gammas.append((day['date'], best['gpts'], size, best['gpts']*size))

        open_prem = sum(p['entry_premium_usd'] for p in positions)
        equity = cash + open_prem - mtm_value + hedge_qty*(hedge_vwap-S)
        prev_eq = daily[-1][2] if daily else equity
        daily.append((day['date'], port_gpts, equity, equity-prev_eq,
                      len(positions), sum(p['contracts'] for p in positions)))
    return daily, entry_gammas


if __name__ == '__main__':
    daily, entry_gammas = run_diag(4.0)
    gptss = [d[1] for d in daily]

    def pct(v, p):
        s = sorted(v); return s[min(len(s)-1, int(len(s)*p))]

    print(f"\n{'='*64}")
    print(f"  GAMMA AGRÉGÉ DU PORTEFEUILLE — baseline actuel (sans budget)")
    print(f"{'='*64}")
    print(f"  jours observés        : {len(daily)}")
    print(f"  gamma agrégé médian   : {pct(gptss,0.50):.1f} pts")
    print(f"  p75                   : {pct(gptss,0.75):.1f} pts")
    print(f"  p90                   : {pct(gptss,0.90):.1f} pts")
    print(f"  p99                   : {pct(gptss,0.99):.1f} pts")
    print(f"  max                   : {max(gptss):.1f} pts")
    for thr in (4, 6, 8):
        frac = sum(1 for g in gptss if g > thr)/len(gptss)*100
        print(f"  % de jours gamma > {thr}  : {frac:.1f}%")

    print(f"\n  --- Gamma INDIVIDUEL à l'entrée (g_pts par option) ---")
    eg = [e[1] for e in entry_gammas]
    print(f"  entrées totales       : {len(eg)}")
    print(f"  g_pts médian          : {pct(eg,0.50):.2f}")
    print(f"  g_pts max             : {max(eg):.2f}")
    print(f"  entrées avec g_pts>6  : {sum(1 for g in eg if g>6)}  "
          f"({sum(1 for g in eg if g>6)/len(eg)*100:.1f}%)")
    print(f"  entrées avec g_pts>4  : {sum(1 for g in eg if g>4)}  "
          f"({sum(1 for g in eg if g>4)/len(eg)*100:.1f}%)")

    # Fenêtre de max drawdown
    eq = [d[2] for d in daily]
    peak = eq[0]; peak_i = 0; max_dd = 0.0; dd_lo_i = 0
    cur_peak_i = 0
    for i, v in enumerate(eq):
        if v > peak:
            peak = v; cur_peak_i = i
        if peak - v > max_dd:
            max_dd = peak - v; dd_lo_i = i; peak_i = cur_peak_i

    print(f"\n{'='*64}")
    print(f"  FENÊTRE DE MAX DRAWDOWN ({max_dd:,.0f} $)")
    print(f"{'='*64}")
    print(f"  pic   : {daily[peak_i][0]}  equity {eq[peak_i]:,.0f}$")
    print(f"  creux : {daily[dd_lo_i][0]}  equity {eq[dd_lo_i][0] if False else eq[dd_lo_i]:,.0f}$")
    print(f"\n  {'date':<12} {'gamma_agg':>10} {'n_pos':>6} {'notion':>7} {'pnl_jour':>10}")
    lo = max(0, peak_i-1); hi = min(len(daily), dd_lo_i+2)
    for d in daily[lo:hi]:
        flag = "  <-- gamma>6" if d[1] > 6 else ""
        print(f"  {str(d[0]):<12} {d[1]:>10.1f} {d[4]:>6} {d[5]:>7.1f} {d[3]:>9,.0f}${flag}")
