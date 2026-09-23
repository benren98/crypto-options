"""
backtest_cb2.py — Élargissement A2 : recherche du meilleur TRIGGER d'allègement
pour le circuit breaker gradué, sur la base de production C1+C2 (skew-pondéré,
min .50, sans « toujours en position »).

On garde keep=0.3 et reprise rapide (move3j<3%), et on balaie le type de signal
qui déclenche l'allègement du palier 1 : horizons de move (1/2/3j), de DVOL (1/3j),
et un spike de HV5. But : minimiser MaxDD en gardant le PnL le plus haut possible.

Usage : python backtest_cb2.py [--years 4]
"""
import sys, math, argparse, io, contextlib
sys.path.insert(0, '.')
import backtest_combo as bc

SKEW_W = bc.SKEW_W
BASE = dict(w=SKEW_W, entry_min=0.50, always_one=False)   # C1+C2

def run(years, grad=False, trig=None):
    return bc.run_combo(years, grad=grad, keep=0.3, t1_restore=3.0, trig=trig, **BASE)

# label, trig dict (None = pas de gradué, juste C1+C2)
CONFIGS = [
    ("C1+C2 (sans gradué)",          None,                               False),
    ("A2 move3=5 | dvol3=6",         {'move3':5.0, 'dvol3':6.0},         True),
    ("move3=5 seul",                 {'move3':5.0},                      True),
    ("dvol3=6 seul",                 {'dvol3':6.0},                      True),
    ("move1=3.5 (gap 1j)",           {'move1':3.5},                      True),
    ("move1=5 (gap 1j fort)",        {'move1':5.0},                      True),
    ("move2=4",                      {'move2':4.0},                      True),
    ("dvol1=4 (spike DVOL 1j)",      {'dvol1':4.0},                      True),
    ("hv5 > 1.3×hv10",               {'hv5x':1.3},                       True),
    ("move1=4 + move3=5 + dvol3=6",  {'move1':4.0,'move3':5.0,'dvol3':6.0}, True),
    ("move1=4 + dvol1=4 (rapide)",   {'move1':4.0,'dvol1':4.0},          True),
    ("move2=4 + dvol3=6",            {'move2':4.0,'dvol3':6.0},          True),
    ("move1=4 + move3=5 + hv5×1.3",  {'move1':4.0,'move3':5.0,'hv5x':1.3}, True),
]

if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('--years', type=float, default=4.0)
    a = ap.parse_args()
    print(f"\n  A2 ÉLARGI — TRIGGERS D'ALLÈGEMENT sur base C1+C2 — BTC, {a.years} ans")
    print(f"  {'Trigger':<32} {'PnL':>9} {'MaxDD':>8} {'Calmar':>7} {'Sharpe':>7} {'PireJ':>8}")
    print(f"  {'-'*32} {'-'*9} {'-'*8} {'-'*7} {'-'*7} {'-'*8}")
    rows = []
    for label, trig, grad in CONFIGS:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = run(a.years, grad=grad, trig=trig)
        rows.append((label, r))
        print(f"  {label:<32} {r['pnl']:>8,.0f}$ {r['maxdd']:>7,.0f}$ "
              f"{r['calmar']:>7.2f} {r['sharpe']:>7.2f} {r['worst']:>7,.0f}$")
    base = rows[0][1]
    best = max(rows[1:], key=lambda x: x[1]['calmar'])
    # meilleur PnL parmi ceux qui baissent le DD d'au moins 25% vs C1+C2
    dd_target = base['maxdd']*0.75
    eligible = [(l,r) for l,r in rows[1:] if r['maxdd'] <= dd_target]
    best_pnl = max(eligible, key=lambda x: x[1]['pnl']) if eligible else None
    print(f"\n  C1+C2 (réf) : PnL {base['pnl']:,.0f}$ · MaxDD {base['maxdd']:,.0f}$ · Calmar {base['calmar']:.2f}")
    print(f"  Meilleur Calmar : {best[0]} · PnL {best[1]['pnl']:,.0f}$ · MaxDD {best[1]['maxdd']:,.0f}$ · Calmar {best[1]['calmar']:.2f}")
    if best_pnl:
        print(f"  Meilleur PnL (DD ≤ −25%) : {best_pnl[0]} · PnL {best_pnl[1]['pnl']:,.0f}$ · MaxDD {best_pnl[1]['maxdd']:,.0f}$ · Calmar {best_pnl[1]['calmar']:.2f}")
