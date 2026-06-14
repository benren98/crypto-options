"""
backtest_gamma.py — À MIN_PREMIUM_USD = 150$, balaie la pénalité gamma
(GAMMA_PEN_START / GAMMA_SCORE_CAP) pour voir si desserrer la pénalité fait
entrer des options court terme (fort theta) et améliore le risque-ajusté.

Config = prod (skew-pondéré + CB gradué). Traque la distribution des TTE pour
vérifier si le court terme (≤7j) apparaît réellement.

Usage : python backtest_gamma.py [--years 4]
"""
import sys, argparse, io, contextlib, collections
sys.path.insert(0, '.')
import backtest as bt
import backtest_combo as bc

SK = bc.SKEW_W
PROD = dict(w=SK, entry_min=0.50, always_one=False, grad=True,
            keep=0.3, t1_restore=3.0, trig={'move1':5.0,'move3':6.0})

def run(start, cap, days=None):
    bt.GAMMA_PEN_START = start
    bt.GAMMA_SCORE_CAP = cap
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = bc.run_combo(4.0, days=days, **PROD)
    return r

# label, gamma_start, gamma_cap
CONFIGS = [
    ("baseline 5 / 10",   5.0, 10.0),
    ("tight 3 / 8",       3.0,  8.0),
    ("loose 8 / 15",      8.0, 15.0),
    ("loose 10 / 20",    10.0, 20.0),
    ("loose 15 / 30",    15.0, 30.0),
    ("loose 20 / 40",    20.0, 40.0),
    ("OFF (100 / 200)", 100.0,200.0),
]

if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('--years', type=float, default=4.0)
    a = ap.parse_args()
    o_s, o_c, o_p = bt.GAMMA_PEN_START, bt.GAMMA_SCORE_CAP, bt.MIN_PREMIUM_USD
    bt.MIN_PREMIUM_USD = 150.0   # fixé comme demandé

    print(f"\n  PÉNALITÉ GAMMA @ prime ≥ 150$ — config prod, BTC, {a.years} ans")
    print(f"  {'Config':<18} {'PnL':>9} {'MaxDD':>8} {'Calmar':>7} {'PireJ':>8} {'entrées':>8} {'TTE distrib (j: n)':>26} {'≤7j':>5}")
    print(f"  {'-'*18} {'-'*9} {'-'*8} {'-'*7} {'-'*8} {'-'*8} {'-'*26} {'-'*5}")
    rows = []
    for label, st_, cap in CONFIGS:
        r = run(st_, cap)
        tte_c = collections.Counter(int(e[0]) for e in r['entries'])
        short = sum(n for t, n in tte_c.items() if t <= 7)
        distrib = " ".join(f"{t}:{n}" for t, n in sorted(tte_c.items()))
        rows.append((label, r, short))
        print(f"  {label:<18} {r['pnl']:>8,.0f}$ {r['maxdd']:>7,.0f}$ {r['calmar']:>7.2f} "
              f"{r['worst']:>7,.0f}$ {len(r['entries']):>8} {distrib:>26} {short:>5}")

    base = rows[0][1]
    best = max(rows, key=lambda x: x[1]['calmar'])
    print(f"\n  Baseline (5/10 @150$) : PnL {base['pnl']:,.0f}$ · MaxDD {base['maxdd']:,.0f}$ · Calmar {base['calmar']:.2f}")
    print(f"  Meilleur Calmar : {best[0]} · PnL {best[1]['pnl']:,.0f}$ · MaxDD {best[1]['maxdd']:,.0f}$ · Calmar {best[1]['calmar']:.2f}")
    bt.GAMMA_PEN_START, bt.GAMMA_SCORE_CAP, bt.MIN_PREMIUM_USD = o_s, o_c, o_p
