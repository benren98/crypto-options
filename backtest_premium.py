"""
backtest_premium.py — Sweep du plancher de prime MIN_PREMIUM_USD ($/BTC au bid),
sur la config de prod (skew-pondéré + CB gradué). Montre l'effet PnL/DD/Calmar ET
le profil (TTE, prime, taille, prime totale encaissée) de ce qui se fait couper.

Question : monter le plancher écarte-t-il du bruit (petites primes ridicules) ou
sacrifie-t-il les options court terme à fort theta ?

Usage : python backtest_premium.py [--years 4]
"""
import sys, argparse, io, contextlib, statistics as st
sys.path.insert(0, '.')
import backtest as bt
import backtest_combo as bc

SK = bc.SKEW_W
PROD = dict(w=SK, entry_min=0.50, always_one=False, grad=True,
            keep=0.3, t1_restore=3.0, trig={'move1':5.0,'move3':6.0})

def med(xs): return st.median(xs) if xs else 0

def run_at(prem, days=None):
    bt.MIN_PREMIUM_USD = prem
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = bc.run_combo(4.0, days=days, **PROD)
    return r

if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('--years', type=float, default=4.0)
    a = ap.parse_args()
    orig = bt.MIN_PREMIUM_USD

    THRESHOLDS = [50, 100, 150, 200, 250, 300, 400]
    print(f"\n  SWEEP PLANCHER DE PRIME ($/BTC) — config prod, BTC, {a.years} ans")
    print(f"  {'seuil':>6} {'PnL':>9} {'MaxDD':>8} {'Calmar':>7} {'PireJ':>8} {'entrées':>8} "
          f"{'TTE méd':>8} {'prime méd':>10} {'totprime':>9}")
    print(f"  {'-'*6} {'-'*9} {'-'*8} {'-'*7} {'-'*8} {'-'*8} {'-'*8} {'-'*10} {'-'*9}")
    rows = []
    for prem in THRESHOLDS:
        r = run_at(prem)
        ent = r['entries']
        ttes  = [e[0] for e in ent]
        prems = [e[1] for e in ent]                 # $/BTC
        totp  = [e[1]*e[2] for e in ent]            # prime totale $ encaissée
        rows.append((prem, r, med(ttes), med(prems), med(totp)))
        print(f"  {prem:>5}\$ {r['pnl']:>8,.0f}\$ {r['maxdd']:>7,.0f}\$ {r['calmar']:>7.2f} "
              f"{r['worst']:>7,.0f}\$ {len(ent):>8} {med(ttes):>7.1f}j {med(prems):>9,.0f}\$ {med(totp):>8,.0f}\$")

    # Profil de ce qui est coupé entre 50$ et 200$ (les entrées présentes à 50 mais absentes à 200)
    e50  = run_at(50)['entries']
    e200 = run_at(200)['entries']
    # approx : trades en plus à 50$ = ceux de faible prime $/BTC
    cut = sorted([e for e in e50 if e[1] < 200], key=lambda e: e[1])
    print(f"\n  ── Profil des entrées à prime < 200\$/BTC (candidates au retrait) ──")
    if cut:
        short = [e for e in cut if e[0] <= 4]
        print(f"  nombre        : {len(cut)}  (dont {len(short)} à TTE ≤ 4j)")
        print(f"  TTE médian    : {med([e[0] for e in cut]):.1f}j")
        print(f"  prime médiane : {med([e[1] for e in cut]):,.0f}\$/BTC")
        print(f"  size médian   : {med([e[2] for e in cut]):.2f} BTC")
        print(f"  prime tot méd : {med([e[1]*e[2] for e in cut]):,.0f}\$ encaissés par position")

    best = max(rows, key=lambda x: x[1]['calmar'])
    print(f"\n  Actuel (50\$) : PnL {rows[0][1]['pnl']:,.0f}\$ · MaxDD {rows[0][1]['maxdd']:,.0f}\$ · Calmar {rows[0][1]['calmar']:.2f}")
    print(f"  Meilleur Calmar : {best[0]}\$ · PnL {best[1]['pnl']:,.0f}\$ · MaxDD {best[1]['maxdd']:,.0f}\$ · Calmar {best[1]['calmar']:.2f}")
    bt.MIN_PREMIUM_USD = orig
