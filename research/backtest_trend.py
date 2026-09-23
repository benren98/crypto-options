"""
backtest_trend.py — A1 : overlay de SIZING tendance/momentum (BTC).

Hypothèse : les gaps baissiers surviennent en down-trend. Réduire la taille quand
le momentum spot est négatif coupe l'exposition AVANT le gap (le circuit breaker,
lui, est réactif). Bidirectionnel possible : augmenter en régime porteur.

Base = production actuelle (convexité 1.5 + CB). On multiplie la taille par un
trend_factor calculé sur les closes (déjà dans la boucle) :

    trend_factor = clamp(1 + k · trend_z, floor, cap)

Signaux testés (trend_z, négatif = baissier) :
    sma20  : S/SMA20 − 1        (norm 5%)
    sma50  : S/SMA50 − 1        (norm 8%)
    mom10  : S/close[-11] − 1   (norm 10%)
    ddhigh : S/max(30j) − 1 ≤0  (norm 10%)  → uniquement baissier par nature

downside_only=True → cap=1.0 (on ne fait que réduire, jamais augmenter).

Usage : python backtest_trend.py [--years 4]
"""
import sys, math, argparse, io, contextlib
sys.path.insert(0, '.')
import backtest as bt

HC = bt.BA_HAIRCUT_VOLPTS

NORM = {'sma20': 0.05, 'sma50': 0.08, 'mom10': 0.10, 'ddhigh': 0.10}


class Trend:
    def __init__(self, name, signal=None, k=0.0, floor=0.5, cap=1.0):
        self.name, self.signal, self.k = name, signal, k
        self.floor, self.cap = floor, cap

    def factor(self, closes, S):
        if self.signal is None:
            return 1.0
        if self.signal == 'sma20':
            if len(closes) < 20: return 1.0
            ref = sum(closes[-20:]) / 20; z = S/ref - 1
        elif self.signal == 'sma50':
            if len(closes) < 50: return 1.0
            ref = sum(closes[-50:]) / 50; z = S/ref - 1
        elif self.signal == 'mom10':
            if len(closes) < 11: return 1.0
            z = S/closes[-11] - 1
        elif self.signal == 'ddhigh':
            if len(closes) < 30: return 1.0
            z = S/max(closes[-30:]) - 1   # ≤ 0
        else:
            return 1.0
        z /= NORM[self.signal]
        return max(self.floor, min(self.cap, 1.0 + self.k * z))


def run_trend(tr: Trend, years=4.0, circuit_breaker=True, always_one=True):
    days = bt.fetch_history(years + 0.15)
    closes_hist, dvol_30, dvol_hist = [], [], []
    positions = []
    hedge_qty = hedge_vwap = 0.0
    cash = 0.0
    equity_curve, daily_pnls, notionals = [], [], []
    n_trades = n_exp_itm = 0
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

        # CB
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

        # expiration
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

        # mark + hedge
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

        # entrée avec overlay tendance
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
                    score   = (0.40*s_ivhv+0.30*s_yield+0.30*s_skew)*g_fac
                    if best is None or score > best['score']:
                        best = {'score': score, 'K': K, 'tte': tte, 'price': price}
            ok = best and (best['score'] >= bt.ENTRY_SCORE_MIN or must_open)
            if ok:
                tf = tr.factor(closes_hist, S)
                size = round(best['score']**bt.SIZE_CONVEXITY * (0.5+0.5*iv_rank) * tf, 1)
                size = max(0.1, min(size, bt.MAX_PORTFOLIO_BTC - used))
                if size >= 0.1:
                    positions.append({'k': best['K'], 'tte_left': best['tte'],
                                      'contracts': size, 'net_prem_usd': best['price']*size})
                    n_trades += 1

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
    worst, wdate = min(daily_pnls)
    return {'name': tr.name, 'pnl': eq[-1], 'maxdd': max_dd, 'sharpe': sharpe,
            'calmar': calmar, 'itm': n_exp_itm, 'worst': worst, 'wdate': wdate,
            'avg_not': sum(notionals)/len(notionals)}


CONFIGS = [
    Trend("0. BASELINE (conv1.5 + CB)"),
    # downside-only (cap=1) : on ne fait que réduire en down-trend
    Trend("sma20 k0.5 floor.5 down",  'sma20', 0.5, 0.5, 1.0),
    Trend("sma20 k1.0 floor.4 down",  'sma20', 1.0, 0.4, 1.0),
    Trend("sma50 k0.7 floor.4 down",  'sma50', 0.7, 0.4, 1.0),
    Trend("mom10 k0.7 floor.4 down",  'mom10', 0.7, 0.4, 1.0),
    Trend("mom10 k1.0 floor.3 down",  'mom10', 1.0, 0.3, 1.0),
    Trend("ddhigh k1.0 floor.4 down", 'ddhigh', 1.0, 0.4, 1.0),
    Trend("ddhigh k1.5 floor.3 down", 'ddhigh', 1.5, 0.3, 1.0),
    # bidirectionnel (cap=1.3) : réduit ET augmente
    Trend("sma20 k0.5 .5-1.3 bidir",  'sma20', 0.5, 0.5, 1.3),
    Trend("mom10 k0.7 .4-1.3 bidir",  'mom10', 0.7, 0.4, 1.3),
]

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--years', type=float, default=4.0)
    a = ap.parse_args()
    print(f"\n  A1 — OVERLAY TENDANCE/MOMENTUM — BTC, conv1.5 + CB, {a.years} ans")
    print(f"  {'Config':<30} {'PnL':>9} {'MaxDD':>8} {'Calmar':>7} {'Sharpe':>7} {'PireJ':>8} {'Not':>5} {'ITM':>4}")
    print(f"  {'-'*30} {'-'*9} {'-'*8} {'-'*7} {'-'*7} {'-'*8} {'-'*5} {'-'*4}")
    rows = []
    for cfg in CONFIGS:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = run_trend(cfg, years=a.years)
        rows.append(r)
        print(f"  {r['name']:<30} {r['pnl']:>8,.0f}$ {r['maxdd']:>7,.0f}$ "
              f"{r['calmar']:>7.2f} {r['sharpe']:>7.2f} {r['worst']:>7,.0f}$ "
              f"{r['avg_not']:>5.1f} {r['itm']:>4}")
    base = rows[0]
    best = max(rows[1:], key=lambda r: r['calmar'])
    print(f"\n  Baseline : PnL {base['pnl']:,.0f}$ · MaxDD {base['maxdd']:,.0f}$ · Calmar {base['calmar']:.2f}")
    print(f"  Meilleur : {best['name']} · PnL {best['pnl']:,.0f}$ · MaxDD {best['maxdd']:,.0f}$ · Calmar {best['calmar']:.2f}")
