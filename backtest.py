"""
backtest.py — Backtest par modèle de la stratégie VRP short put delta-hedgée.

Données réelles : spot BTC (perpetual daily) + DVOL (index de vol ATM Deribit).
Prix d'options reconstruits en Black-Scholes :
    IV(strike) = DVOL × (1 + SKEW_SLOPE × OTM%)   — skew calibré sur juin 2026
    prix de vente = BS(mark_iv) − haircut bid (BA_HAIRCUT_VOLPTS pts de vol)

Règles rejouées (identiques à greeks_hedge.py) :
    - score v2 : 0.40×s_iv_hv(HV blend) + 0.30×s_yield(×z) + 0.30×s_skew, pénalité gamma
    - seuil 0.45, DVOL ≥ 35%, sizing = score × (0.5+0.5×rank), cap 5 BTC
    - delta hedge via perp, rebalance si drift > seuil dépendant de l'IV
    - règle "toujours ≥ 1 position" : si portefeuille vide, on prend le meilleur score
    - expiration : règlement au payoff, position retirée

Usage : python backtest.py [--years 4] [--no-floor] (--no-floor désactive la règle ≥1 position)
"""
import sys, math, argparse
sys.path.insert(0, '.')
from datetime import datetime, timezone
from greeks_hedge import get, now_ms

# ── Paramètres stratégie (miroir de greeks_hedge.py) ──────────────────────────
ENTRY_SCORE_MIN   = 0.45
MAX_PORTFOLIO_BTC = 5.0
GAMMA_PEN_START   = 5.0
GAMMA_SCORE_CAP   = 10.0
DVOL_MIN          = 35.0
YIELD_NORM        = 0.30
SKEW_NORM         = 0.20
MIN_PREMIUM_USD   = 50.0    # plancher de prime au bid ($/BTC) — anti-poussière (remplace le filtre B/A%)

# ── Paramètres modèle de pricing ───────────────────────────────────────────────
SKEW_SLOPE        = 0.013   # IV(K) = DVOL × (1 + 0.013 × OTM%) — calibré juin 2026 (~1.3%/pt OTM)
BA_HAIRCUT_VOLPTS = 1.5     # on vend au bid ≈ mark_iv − 1.5 pts de vol
FUNDING_DAILY     = 0.0001  # ~0.01%/jour payé sur le short perp (hedge)
TTE_CHOICES       = [3, 7, 14, 21]       # échéances candidates (jours) — inclut le court terme
# Deltas candidats : plancher retiré (SCAN_DELTA_MAX=0) → on inclut les far-OTM petits deltas.
# Le plancher de prime ($50) écarte ensuite ceux trop bon marché.
DELTA_TARGETS     = [-0.05, -0.08, -0.12, -0.16, -0.20, -0.25]

N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
n_pdf = lambda x: math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

def bs_put(S, K, T, sigma):
    """Prix put BS (en $), delta, gamma. T en années, sigma en décimal."""
    if T <= 0:
        return max(K - S, 0.0), -1.0 if S < K else 0.0, 0.0
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / sq
    d2 = d1 - sq
    price = K * N(-d2) - S * N(-d1)
    delta = N(d1) - 1.0
    gamma = n_pdf(d1) / (S * sq)
    return price, delta, gamma

def strike_for_delta(S, T, sigma_atm, target_delta):
    """Strike donnant ~target_delta, avec skew appliqué itérativement."""
    K = S * 0.93
    for _ in range(40):
        otm = (S - K) / S * 100
        sig = sigma_atm * (1 + SKEW_SLOPE * otm)
        _, d, _ = bs_put(S, K, T, sig)
        if abs(d - target_delta) < 0.002:
            break
        K *= 1 + (target_delta - d) * 0.08   # delta plus négatif → strike plus haut
    return K

# ── Données historiques ────────────────────────────────────────────────────────
def fetch_history(years: float):
    end_ts   = now_ms()
    start_ts = end_ts - int(years * 365 * 24 * 3600 * 1000)
    spot_d = get('get_tradingview_chart_data', {
        'instrument_name': 'BTC-PERPETUAL',
        'start_timestamp': start_ts, 'end_timestamp': end_ts, 'resolution': '1D'})
    dvol_d = get('get_volatility_index_data', {
        'currency': 'BTC', 'start_timestamp': start_ts, 'end_timestamp': end_ts, 'resolution': '1D'})
    dvol_by_day = {datetime.fromtimestamp(r[0]/1000, tz=timezone.utc).date(): r[4] for r in dvol_d['data']}
    days = []
    for tick, close in zip(spot_d['ticks'], spot_d['close']):
        d = datetime.fromtimestamp(tick/1000, tz=timezone.utc).date()
        if d in dvol_by_day and close:
            days.append({'date': d, 'spot': close, 'dvol': dvol_by_day[d]})
    return days

def hv_from(closes, n):
    if len(closes) < n + 1:
        return None
    w = closes[-(n+1):]
    rets = [math.log(w[i]/w[i-1]) for i in range(1, len(w))]
    return math.sqrt(sum(r*r for r in rets)/len(rets)) * math.sqrt(365) * 100

# ── Backtest ───────────────────────────────────────────────────────────────────
def rank_mult_linear(iv_rank: float) -> float:
    """Multiplicateur actuel : monotone 0.5 -> 1.0."""
    return 0.5 + 0.5 * iv_rank

def rank_mult_bell(iv_rank: float) -> float:
    """Profil en cloche : 0.5 en bas de range, pic 1.0 vers rank 0.65,
    réduit à 0.6 à l'extrême haut (crash en cours / imminent)."""
    if iv_rank <= 0.65:
        return 0.5 + 0.5 * (iv_rank / 0.65)
    return 1.0 - 0.4 * (iv_rank - 0.65) / 0.35

# ── Circuit breaker ────────────────────────────────────────────────────────────
CB_MOVE_3D_PCT   = 8.0    # déclenche : |move spot 3j| > 8%
CB_DVOL_3D_PTS   = 10.0   # ou DVOL +10 pts en 3j
CB_REENTRY_MOVE  = 4.0    # re-entrée : |move 3j| < 4%
# + condition re-entrée : HV5 < HV10 (le réalisé court se retourne)

def run(years: float, always_one: bool = True, rank_mult=rank_mult_linear,
        circuit_breaker: bool = False, label: str = "", verbose: bool = False):
    days = fetch_history(years + 0.15)   # marge pour warmup HV30
    closes_hist = []
    positions = []      # {strike, tte_left, contracts, entry_premium_usd, iv_entry}
    hedge_qty = 0.0     # BTC short (positif = short)
    hedge_vwap = 0.0
    cash = 0.0          # PnL cumulé réalisé ($)
    equity_curve = []
    n_trades = n_expired_itm = 0
    worst_days = []
    notionals = []
    dvol_30 = []
    dvol_hist = []
    risk_off = False
    n_cb_triggers = 0
    cb_days_off = 0

    for day in days:
        S, dvol = day['spot'], day['dvol']
        closes_hist.append(S)
        dvol_30.append(dvol)
        dvol_30 = dvol_30[-30:]
        dvol_hist.append(dvol)
        hv10, hv30 = hv_from(closes_hist, 10), hv_from(closes_hist, 30)
        if hv10 is None or hv30 is None or len(dvol_30) < 10:
            continue
        hv5 = hv_from(closes_hist, 5)
        hv_blend = 0.5 * hv10 + 0.5 * hv30
        iv_rank  = max(0.0, min(1.0, (dvol - min(dvol_30)) / max(max(dvol_30) - min(dvol_30), 5)))
        move_3d  = abs(S / closes_hist[-4] - 1) * 100 if len(closes_hist) >= 4 else 0.0
        dvol_chg_3d = dvol - dvol_hist[-4] if len(dvol_hist) >= 4 else 0.0

        day_pnl = 0.0

        # ── 0. Circuit breaker ────────────────────────────────────────────────
        if circuit_breaker:
            move_3d_signed = (S / closes_hist[-4] - 1) * 100 if len(closes_hist) >= 4 else 0.0
            if not risk_off and positions and (move_3d_signed < -CB_MOVE_3D_PCT or dvol_chg_3d > CB_DVOL_3D_PTS):
                # Tout racheter au mark + haircut (on paie le spread en sortie)
                for p in positions:
                    T = p['tte_left'] / 365
                    otm = (S - p['strike']) / S * 100
                    sig = (dvol * (1 + SKEW_SLOPE * max(otm, 0)) + BA_HAIRCUT_VOLPTS) / 100
                    price, _, _ = bs_put(S, p['strike'], T, sig)
                    cash += p['entry_premium_usd'] - price * p['contracts']
                    day_pnl += p['entry_premium_usd'] - price * p['contracts']
                positions = []
                # Hedge à plat : réalise le PnL du short
                if hedge_qty != 0:
                    cash += hedge_qty * (hedge_vwap - S)
                    day_pnl += hedge_qty * (hedge_vwap - S)
                    hedge_qty, hedge_vwap = 0.0, 0.0
                risk_off = True
                n_cb_triggers += 1
            elif risk_off:
                cb_days_off += 1
                # Re-entrée : réalisé court se retourne + spot stabilisé
                if hv5 is not None and hv5 < hv10 and move_3d < CB_REENTRY_MOVE:
                    risk_off = False

        # ── 1. Vieillissement + expiration des positions ──────────────────────
        still = []
        for p in positions:
            p['tte_left'] -= 1
            if p['tte_left'] <= 0:
                payoff = max(p['strike'] - S, 0.0) * p['contracts']
                cash += p['entry_premium_usd'] - payoff
                day_pnl += p['entry_premium_usd'] - payoff
                if payoff > 0:
                    n_expired_itm += 1
            else:
                still.append(p)
        positions = still

        # ── 2. Mark-to-model + delta net ──────────────────────────────────────
        net_delta = 0.0
        mtm_value = 0.0     # valeur de rachat des puts vendus ($, négatif pour nous)
        for p in positions:
            T = p['tte_left'] / 365
            otm = (S - p['strike']) / S * 100
            sig = dvol/100 * (1 + SKEW_SLOPE * max(otm, 0))
            price, delta, gamma = bs_put(S, p['strike'], T, sig)
            net_delta += delta * p['contracts']
            mtm_value += price * p['contracts']

        # ── 3. Hedge : rebalance si drift > seuil (5%/IV-dépendant simplifié) ─
        target_short = -net_delta            # short perp = +delta des puts vendus
        drift = abs(target_short - hedge_qty)
        thr = max(0.03, min(0.08, 0.05 * 60 / max(dvol, 20)))   # seuil ~3-8% selon IV
        if drift > thr * max(sum(p['contracts'] for p in positions), 1):
            # PnL réalisé sur la part fermée/ouverte au prix courant
            dq = target_short - hedge_qty
            if hedge_qty != 0 and (dq * hedge_qty < 0):   # réduction → réalise PnL
                closed = min(abs(dq), abs(hedge_qty)) * (1 if hedge_qty > 0 else -1)
                cash += closed * (hedge_vwap - S)          # short: gain si S < vwap
                day_pnl += closed * (hedge_vwap - S)
            if target_short != 0:
                if hedge_qty * target_short > 0 and abs(target_short) > abs(hedge_qty):
                    add = target_short - hedge_qty
                    hedge_vwap = (hedge_vwap * abs(hedge_qty) + S * abs(add)) / abs(target_short)
                elif hedge_qty * target_short <= 0:
                    hedge_vwap = S
            hedge_qty = target_short

        # Funding sur le short perp
        cash -= abs(hedge_qty) * S * FUNDING_DAILY
        day_pnl -= abs(hedge_qty) * S * FUNDING_DAILY

        # ── 4. Entrées (scan + score v2) ──────────────────────────────────────
        used = sum(p['contracts'] for p in positions)
        must_open = always_one and not positions and not risk_off
        if not risk_off and ((dvol >= DVOL_MIN and used < MAX_PORTFOLIO_BTC) or must_open):
            best = None
            for tte in TTE_CHOICES:
                T = tte / 365
                atm_iv = dvol
                for td in DELTA_TARGETS:
                    K = strike_for_delta(S, T, dvol/100, td)
                    otm = (S - K) / S * 100
                    if otm < 2:
                        continue
                    mark_iv = dvol * (1 + SKEW_SLOPE * otm)
                    bid_iv  = mark_iv - BA_HAIRCUT_VOLPTS
                    price, delta, gamma = bs_put(S, K, T, bid_iv/100)
                    if price < MIN_PREMIUM_USD:   # plancher de prime ($/BTC au bid)
                        continue
                    yield_a = (price / S) / T
                    s_ivhv  = max(0.0, min(1.0, bid_iv / hv_blend - 1.0))
                    z       = (otm/100) / max(hv_blend/100 * math.sqrt(T), 1e-9)
                    s_yield = min(1.0, yield_a * z / YIELD_NORM)
                    skew    = bid_iv / atm_iv - 1.0
                    s_skew  = max(0.0, min(1.0, skew / SKEW_NORM))
                    g_pts   = gamma * S * 0.01 * 100
                    g_fac   = max(0.0, 1.0 - max(0.0, g_pts - GAMMA_PEN_START) / (GAMMA_SCORE_CAP - GAMMA_PEN_START))
                    score   = (0.40*s_ivhv + 0.30*s_yield + 0.30*s_skew) * g_fac
                    if best is None or score > best['score']:
                        best = {'score': score, 'K': K, 'tte': tte, 'price': price, 'otm': otm}
            ok = best and (best['score'] >= ENTRY_SCORE_MIN or must_open)
            if ok:
                size = round(best['score'] * rank_mult(iv_rank), 1)
                size = max(0.1, min(size, MAX_PORTFOLIO_BTC - used))
                if size >= 0.1:
                    positions.append({
                        'strike': best['K'], 'tte_left': best['tte'], 'contracts': size,
                        'entry_premium_usd': best['price'] * size,
                    })
                    n_trades += 1

        # ── 5. Equity = cash + prime des positions ouvertes − valeur de rachat
        open_prem = sum(p['entry_premium_usd'] for p in positions)
        hedge_mtm = hedge_qty * (hedge_vwap - S)   # short flottant
        equity = cash + open_prem - mtm_value + hedge_mtm
        eq_prev = equity_curve[-1][1] if equity_curve else equity
        equity_curve.append((day['date'], equity, equity - eq_prev, S, dvol))
        worst_days.append((equity - eq_prev, day['date'], S, dvol))
        notional_track = sum(p['contracts'] for p in positions)
        notionals.append(notional_track)

    # ── Stats ──────────────────────────────────────────────────────────────────
    eq = [e[1] for e in equity_curve]
    rets = [eq[i] - eq[i-1] for i in range(1, len(eq))]
    peak, max_dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        max_dd = max(max_dd, peak - v)
    mean_d = sum(rets)/len(rets)
    std_d  = (sum((r-mean_d)**2 for r in rets)/len(rets)) ** 0.5
    sharpe = mean_d / std_d * math.sqrt(365) if std_d > 0 else 0
    worst_days.sort()

    print(f"\n{'='*70}")
    print(f"  BACKTEST {equity_curve[0][0]} -> {equity_curve[-1][0]}  ({len(equity_curve)} jours)")
    print(f"  Regle >=1 position : {'ON' if always_one else 'OFF'}")
    print(f"{'='*70}")
    print(f"  PnL final        : {eq[-1]:>12,.0f} $")
    print(f"  PnL annualise    : {eq[-1]/len(eq)*365:>12,.0f} $/an  (sur notionnel max 5 BTC)")
    print(f"  Max drawdown     : {max_dd:>12,.0f} $")
    print(f"  Sharpe (daily)   : {sharpe:>12.2f}")
    print(f"  Trades           : {n_trades}  |  expires ITM : {n_expired_itm}")
    if circuit_breaker:
        print(f"  Circuit breaker  : {n_cb_triggers} déclenchements  |  {cb_days_off} jours risk-off")
    avg_not = sum(notionals)/len(notionals)
    avg_spot = sum(e[3] for e in equity_curve)/len(equity_curve)
    print(f"  Notionnel moyen  : {avg_not:.1f} BTC (~{avg_not*avg_spot:,.0f} $)  ->  rendement ~{eq[-1]/len(eq)*365/(avg_not*avg_spot)*100:.1f}%/an du notionnel")
    print(f"\n  10 pires jours :")
    for pnl, d, s, dv in worst_days[:10]:
        print(f"    {d}  {pnl:>10,.0f} $   spot {s:>10,.0f}  DVOL {dv:.0f}%")
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
    ap.add_argument('--no-floor', action='store_true', help='desactive la regle toujours >=1 position')
    a = ap.parse_args()
    run(a.years, always_one=not a.no_floor)
