"""
backtest_sizing.py — Banc d'essai des schémas de SIZING (BTC seul).

Réutilise le pricing/données/règles de backtest.py et ne réécrit que la boucle
pour pouvoir injecter, au moment de l'entrée :
    - le gamma dollar agrégé du portefeuille (budget gamma),
    - le régime de vol (vol-target),
    - la position dans la drawdown (dd-throttle),
    - un profil de rank linéaire/cloche, et une convexité sur le score.

Objectif : maximiser le Calmar (PnL annualisé / MaxDD) — réduire le DD pour
améliorer le PnL net ajusté du risque.

Usage : python backtest_sizing.py [--years 4]
"""
import sys, math, argparse, io, contextlib
sys.path.insert(0, '.')
import backtest as bt

# ── Schéma de sizing ────────────────────────────────────────────────────────────
class Sizing:
    """Configuration d'un schéma de sizing. Tous les leviers sont combinables."""
    def __init__(self, name, rank='linear', vol_target=None, gamma_budget=None,
                 gamma_budget_usd=None, convex=1.0, dd_throttle=None, cap_btc=5.0,
                 vol_cap=None):
        self.name             = name
        self.rank             = rank      # 'linear' | 'bell' | 'flat'
        self.vol_target       = vol_target      # ex 50.0 → size *= clamp(50/HV_blend)
        self.gamma_budget     = gamma_budget    # plafond gamma agrégé en gpts (relatif notionnel)
        self.gamma_budget_usd = gamma_budget_usd # plafond gamma agrégé en $ (delta-$ / move 1%)
        self.convex           = convex          # exposant sur le score (1=linéaire, 2=concentre)
        self.dd_throttle      = dd_throttle      # k : size *= max(0.3, 1 - k*dd_frac)
        self.cap_btc          = cap_btc
        self.vol_cap          = vol_cap          # (dvol_thr, cap_reduit) : cap dynamique en stress

    def rank_mult(self, iv_rank):
        if self.rank == 'bell':
            return bt.rank_mult_bell(iv_rank)
        if self.rank == 'flat':
            return 1.0
        return 0.5 + 0.5 * iv_rank

    def raw_size(self, score, iv_rank, hv_blend, dd_frac):
        base = (score ** self.convex) * self.rank_mult(iv_rank)
        if self.vol_target:
            base *= max(0.30, min(1.50, self.vol_target / max(hv_blend, 1e-6)))
        if self.dd_throttle:
            base *= max(0.30, 1.0 - self.dd_throttle * dd_frac)
        return base


def run_sized(sz: Sizing, years=4.0, always_one=True, circuit_breaker=True, quiet=True):
    days = bt.fetch_history(years + 0.15)
    closes_hist, dvol_30, dvol_hist = [], [], []
    positions = []
    hedge_qty = hedge_vwap = 0.0
    cash = 0.0
    equity_curve, worst_days, notionals = [], [], []
    n_trades = n_expired_itm = 0
    risk_off = False
    n_cb = cb_off = 0
    peak_eq = 0.0
    n_gamma_binds = 0      # entrées où le budget gamma a réduit la taille
    n_binds_below_cap = 0  # ... et où le cap notionnel n'était PAS encore atteint

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

        # ── 0. Circuit breaker ──
        if circuit_breaker:
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
                risk_off = True; n_cb += 1
            elif risk_off:
                cb_off += 1
                if hv5 is not None and hv5 < hv10 and move_3d < bt.CB_REENTRY_MOVE:
                    risk_off = False

        # ── 1. Expiration ──
        still = []
        for p in positions:
            p['tte_left'] -= 1
            if p['tte_left'] <= 0:
                payoff = max(p['strike']-S, 0.0)*p['contracts']
                cash += p['entry_premium_usd'] - payoff
                if payoff > 0: n_expired_itm += 1
            else:
                still.append(p)
        positions = still

        # ── 2. Mark-to-model + gamma agrégé (gpts ET $) ──
        net_delta = mtm_value = port_gpts = port_gusd = 0.0
        for p in positions:
            T = p['tte_left']/365; otm = (S-p['strike'])/S*100
            sig = dvol/100*(1+bt.SKEW_SLOPE*max(otm,0))
            price, delta, gamma = bt.bs_put(S, p['strike'], T, sig)
            net_delta += delta*p['contracts']
            mtm_value += price*p['contracts']
            port_gpts += gamma*S*0.01*100 * p['contracts']      # drift delta % notionnel / 1%
            port_gusd += gamma*p['contracts'] * S*S*0.01         # drift delta-$ / move 1% spot

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

        # ── 4. Entrée avec sizing paramétré ──
        used = sum(p['contracts'] for p in positions)
        # Cap dynamique : réduit en régime stressé (vol_cap = (dvol_thr, cap_reduit))
        eff_cap = sz.cap_btc
        if sz.vol_cap is not None and dvol > sz.vol_cap[0]:
            eff_cap = sz.vol_cap[1]
        must_open = always_one and not positions and not risk_off
        if not risk_off and ((dvol >= bt.DVOL_MIN and used < eff_cap) or must_open):
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
                        best = {'score': score, 'K': K, 'tte': tte, 'price': price,
                                'otm': otm, 'gpts': g_pts, 'gamma': gamma}
            ok = best and (best['score'] >= bt.ENTRY_SCORE_MIN or must_open)
            if ok:
                dd_frac = (peak_eq - (equity_curve[-1][1] if equity_curve else 0.0)) / max(peak_eq, 1.0) if peak_eq > 0 else 0.0
                raw = sz.raw_size(best['score'], iv_rank, hv_blend, dd_frac)
                size = round(raw, 1)
                cap_room = eff_cap - used
                size_before = max(0.1, min(size, cap_room))
                size = size_before
                # Budget gamma en gpts (relatif notionnel)
                if sz.gamma_budget is not None and best['gpts'] > 1e-9:
                    room = (sz.gamma_budget - port_gpts) / best['gpts']
                    size = min(size, max(0.0, round(room, 1)))
                # Budget gamma en $ (delta-$ par move de 1%) — cohérent dans le temps
                if sz.gamma_budget_usd is not None:
                    cand_gusd = best['gamma'] * S*S*0.01   # par BTC
                    if cand_gusd > 1e-9:
                        room = (sz.gamma_budget_usd - port_gusd) / cand_gusd
                        size = min(size, max(0.0, round(room, 1)))
                # Le budget gamma a-t-il mordu, et avant le cap notionnel ?
                if size < size_before - 1e-9:
                    n_gamma_binds += 1
                    if used < eff_cap - 0.05:   # il restait de la place notionnelle
                        n_binds_below_cap += 1
                if size >= 0.1:
                    positions.append({'strike': best['K'], 'tte_left': best['tte'],
                                      'contracts': size, 'entry_premium_usd': best['price']*size})
                    n_trades += 1

        # ── 5. Equity ──
        open_prem = sum(p['entry_premium_usd'] for p in positions)
        hedge_mtm = hedge_qty*(hedge_vwap-S)
        equity = cash + open_prem - mtm_value + hedge_mtm
        peak_eq = max(peak_eq, equity)
        eq_prev = equity_curve[-1][1] if equity_curve else equity
        equity_curve.append((day['date'], equity, equity-eq_prev, S, dvol))
        worst_days.append((equity-eq_prev, day['date'], S, dvol))
        notionals.append(sum(p['contracts'] for p in positions))

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
    avg_not = sum(notionals)/len(notionals)
    return {'name': sz.name, 'pnl': eq[-1], 'pnl_an': pnl_an, 'maxdd': max_dd,
            'sharpe': sharpe, 'calmar': calmar, 'trades': n_trades,
            'itm': n_expired_itm, 'ncb': n_cb, 'cboff': cb_off, 'avg_not': avg_not,
            'binds': n_gamma_binds, 'binds_below_cap': n_binds_below_cap,
            'curve': equity_curve}


# Budget $ : gpts 6 ≈ 0.06 BTC de drift delta / 1%. À S~70k → ~0.06×70000 = 4 200 $/1%.
# On teste plusieurs seuils $ constants dans le temps.
CONFIGS = [
    Sizing("0. BASELINE (cap5)"),
    Sizing("A. Conv1.5 (cap5)",                   convex=1.5),
    Sizing("K. Conv1.5 + gbud_gpts6 (cap5)",      convex=1.5, gamma_budget=6.0),
    Sizing("U3. Conv1.5 + gbud$ 3000 (cap5)",     convex=1.5, gamma_budget_usd=3000.0),
    Sizing("U4. Conv1.5 + gbud$ 4000 (cap5)",     convex=1.5, gamma_budget_usd=4000.0),
    Sizing("U5. Conv1.5 + gbud$ 5000 (cap5)",     convex=1.5, gamma_budget_usd=5000.0),
    # Réduire le cap SEULEMENT en régime stressé (DVOL élevé) — pas d'augmentation
    Sizing("V60. Conv1.5 + cap3.5 si DVOL>60",    convex=1.5, vol_cap=(60.0, 3.5)),
    Sizing("V55. Conv1.5 + cap3.5 si DVOL>55",    convex=1.5, vol_cap=(55.0, 3.5)),
    Sizing("V55b Conv1.5 + cap3 si DVOL>55",      convex=1.5, vol_cap=(55.0, 3.0)),
    # Combo gamma$ + cap dynamique
    Sizing("W. Conv1.5 + gbud$4000 + cap3.5@55",  convex=1.5, gamma_budget_usd=4000.0, vol_cap=(55.0, 3.5)),
]

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--years', type=float, default=4.0)
    a = ap.parse_args()

    print(f"\n  Banc d'essai SIZING — BTC seul, cap notionnel = 5 BTC (jamais augmenté), CB ON, {a.years} ans")
    print(f"  {'Config':<32} {'PnL':>9} {'MaxDD':>8} {'Calmar':>7} {'Sharpe':>7} {'Not':>5} {'binds':>6} {'<cap':>5}")
    print(f"  {'-'*32} {'-'*9} {'-'*8} {'-'*7} {'-'*7} {'-'*5} {'-'*6} {'-'*5}")
    results = []
    for cfg in CONFIGS:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = run_sized(cfg, years=a.years)
        results.append(r)
        print(f"  {r['name']:<32} {r['pnl']:>8,.0f}$ {r['maxdd']:>7,.0f}$ "
              f"{r['calmar']:>7.2f} {r['sharpe']:>7.2f} {r['avg_not']:>5.1f} "
              f"{r['binds']:>6} {r['binds_below_cap']:>5}")
    best = max(results, key=lambda r: r['calmar'])
    print(f"\n  binds = entrées où le budget gamma a réduit la taille")
    print(f"  <cap  = ... ET où le cap notionnel de 5 BTC n'était PAS encore atteint")
    print(f"          (preuve d'une protection gamma-spécifique, pas un simple plafond de taille)")
    print(f"\n  >> Meilleur Calmar : {best['name']}  "
          f"(PnL {best['pnl']:,.0f}$, MaxDD {best['maxdd']:,.0f}$, Calmar {best['calmar']:.2f})")
