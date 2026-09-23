"""
backtest_threshold.py — Re-calibre ENTRY_SCORE_MIN après le passage aux poids
0.20/0.15/0.65. Avec le skew dominant + s_skew qui sature, les scores montent →
le seuil 0.50 ne filtre presque plus. On balaie le seuil sous DEUX mondes :
  • skew RÉEL fité (surface du jour, in-memory) — ce que voit le live
  • modèle LINÉAIRE (backtest historique actuel)

Usage : python backtest_threshold.py
"""
import sys, io, contextlib, math
sys.path.insert(0, '.')
import numpy as np
import backtest as bt
import vol_surface_data as vs

def stats_and_trades(run_out, ec):
    eq = [e[1] for e in ec]
    peak, dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v); dd = max(dd, peak - v)
    worst = min(ec[i][2] for i in range(1, len(ec)))
    cal = eq[-1]/len(eq)*365/dd if dd > 0 else 0
    ntr = [l for l in run_out.splitlines() if "Trades" in l]
    n = ntr[0].split(":")[1].split("|")[0].strip() if ntr else "?"
    return eq[-1], dd, cal, worst, n

def run():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ec = bt.run(4.0, circuit_breaker=True)
    return stats_and_trades(buf.getvalue(), ec)

# Fit du skew sur la surface du jour
otm, ratio = [], []
for _, _, mny, iv, atm in vs.all_points():
    if mny and iv and atm and mny < 1:
        otm.append((1-mny)*100); ratio.append(iv/atm)
otm = np.array(otm); ratio = np.array(ratio)
(a, b), *_ = np.linalg.lstsq(np.column_stack([otm, otm**2]), ratio-1, rcond=None)
print(f"  skew réel fité : 1 + {a:.4f}·OTM + {b:.6f}·OTM²   (poids score {bt.SCORE_W_IVHV}/{bt.SCORE_W_YIELD}/{bt.SCORE_W_SKEW})")

THRESHOLDS = [0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75]
o_min, o_a, o_b = bt.ENTRY_SCORE_MIN, bt.SKEW_A, bt.SKEW_B

for lab, skew in [("SKEW RÉEL FITÉ (ce que voit le live)", (a, b)),
                  ("MODÈLE LINÉAIRE (backtest historique)", (bt.SKEW_SLOPE, 0.0))]:
    bt.SKEW_A, bt.SKEW_B = skew
    print(f"\n  {'='*64}\n  {lab}\n  {'='*64}")
    print(f"  {'seuil':>6} {'PnL':>9} {'MaxDD':>8} {'Calmar':>7} {'PireJ':>8} {'trades':>7}")
    print(f"  {'-'*6} {'-'*9} {'-'*8} {'-'*7} {'-'*8} {'-'*7}")
    rows = []
    for thr in THRESHOLDS:
        bt.ENTRY_SCORE_MIN = thr
        pnl, dd, cal, w, n = run()
        rows.append((thr, pnl, dd, cal, w, n))
        print(f"  {thr:>5.2f} {pnl:>8,.0f}$ {dd:>7,.0f}$ {cal:>7.2f} {w:>7,.0f}$ {n:>7}")
    best = max(rows, key=lambda r: r[3])
    print(f"  → meilleur Calmar : seuil {best[0]:.2f} (Calmar {best[3]:.2f}, PnL {best[1]:,.0f}$, {best[5]} trades)")

bt.ENTRY_SCORE_MIN, bt.SKEW_A, bt.SKEW_B = o_min, o_a, o_b
print(f"\n  Rappel : skew fité = 1 jour calme. Le live opère sous vrai skew → table du haut pertinente.")
print(f"  Seuil actuel en prod : {o_min}")
