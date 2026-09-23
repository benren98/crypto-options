"""
backtest_fitted_preview.py — APERÇU : fitte le skew sur la surface réelle d'AUJOURD'HUI
(in-memory, n'écrit pas vol_model_fit.json) et rejoue le backtest + les sweeps de
paramètres sous ce skew convexe réaliste, pour voir si les choix de calibration changent.

⚠ 1 seul jour de données = skew du régime actuel (calme), sans variation de terme/régime.
Indicatif de la DIRECTION de l'impact du risque modèle, pas définitif.

Usage : python backtest_fitted_preview.py
"""
import sys, io, contextlib, math
sys.path.insert(0, '.')
import numpy as np
import backtest as bt
import vol_surface_data as vs

def stats(ec):
    eq = [e[1] for e in ec]
    peak, dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v); dd = max(dd, peak - v)
    worst = min(ec[i][2] for i in range(1, len(ec)))
    cal = eq[-1]/len(eq)*365/dd if dd > 0 else 0
    return eq[-1], dd, cal, worst

def run_bt():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ec = bt.run(4.0, circuit_breaker=True)
    return stats(ec)

# ── 1. Fit du skew sur la surface d'aujourd'hui ──
otm, ratio = [], []
for d, dte, mny, iv, atm in vs.all_points():
    if mny is None or iv is None or not atm or mny >= 1.0:
        continue
    otm.append((1.0 - mny)*100); ratio.append(iv/atm)
otm = np.array(otm); ratio = np.array(ratio)
X = np.column_stack([otm, otm**2])
(a, b), *_ = np.linalg.lstsq(X, ratio - 1.0, rcond=None)
pred = 1 + a*otm + b*otm**2
r2 = 1 - np.sum((ratio-pred)**2)/np.sum((ratio-ratio.mean())**2)

print(f"\n  FIT SKEW (surface réelle du jour, {len(otm)} points, R²={r2:.3f})")
print(f"  réel  : 1 + {a:.4f}·OTM% + {b:.6f}·OTM%²")
print(f"  modèle: 1 + 0.0130·OTM%  (linéaire)")
print(f"  {'OTM%':>5} {'réel':>7} {'modèle':>7}")
for o in (5,10,15,20,25):
    print(f"  {o:>4}% ×{1+a*o+b*o*o:>6.3f} ×{1+0.013*o:>6.3f}")

LIN = (bt.SKEW_SLOPE, 0.0)
FIT = (a, b)

def set_skew(s): bt.SKEW_A, bt.SKEW_B = s

# ── 2. Baseline : modèle linéaire vs skew réel fité (config prod) ──
print(f"\n  {'='*60}\n  BASELINE prod : modèle linéaire vs skew réel fité\n  {'='*60}")
print(f"  {'skew':<12} {'PnL':>9} {'MaxDD':>8} {'Calmar':>7} {'PireJ':>8}")
for lab, s in [("linéaire", LIN), ("réel fité", FIT)]:
    set_skew(s)
    pnl, dd, cal, w = run_bt()
    print(f"  {lab:<12} {pnl:>8,.0f}$ {dd:>7,.0f}$ {cal:>7.2f} {w:>7,.0f}$")

# ── 3. Sweeps de paramètres SOUS le skew réel fité ──
set_skew(FIT)
o_w = (bt.SCORE_W_IVHV, bt.SCORE_W_YIELD, bt.SCORE_W_SKEW)
o_g = (bt.GAMMA_PEN_START, bt.GAMMA_SCORE_CAP)
o_p, o_c = bt.MIN_PREMIUM_USD, bt.SIZE_CONVEXITY

def sweep(title, setter, configs):
    print(f"\n  ── {title} (sous skew réel) ──")
    print(f"  {'config':<26} {'PnL':>9} {'MaxDD':>8} {'Calmar':>7} {'PireJ':>8}")
    for lab, val in configs:
        setter(val)
        pnl, dd, cal, w = run_bt()
        print(f"  {lab:<26} {pnl:>8,.0f}$ {dd:>7,.0f}$ {cal:>7.2f} {w:>7,.0f}$")
    # restore
    bt.SCORE_W_IVHV, bt.SCORE_W_YIELD, bt.SCORE_W_SKEW = o_w
    bt.GAMMA_PEN_START, bt.GAMMA_SCORE_CAP = o_g
    bt.MIN_PREMIUM_USD, bt.SIZE_CONVEXITY = o_p, o_c

def set_w(w):
    bt.SCORE_W_IVHV, bt.SCORE_W_YIELD, bt.SCORE_W_SKEW = w
sweep("Pondérations score (IVHV/yield/skew)", set_w, [
    ("0.30/0.25/0.45 (actuel)", (0.30,0.25,0.45)),
    ("0.25/0.20/0.55 (+skew)",  (0.25,0.20,0.55)),
    ("0.20/0.15/0.65 (++skew)", (0.20,0.15,0.65)),
    ("0.45/0.25/0.30 (+ivhv)",  (0.45,0.25,0.30)),
    ("0.30/0.40/0.30 (+yield)", (0.30,0.40,0.30)),
    ("0.40/0.30/0.30 (ancien)", (0.40,0.30,0.30)),
])

def set_g(g):
    bt.GAMMA_PEN_START, bt.GAMMA_SCORE_CAP = g
sweep("Pénalité gamma", set_g, [
    ("5/10 (actuel)", (5.0,10.0)),
    ("3/8 (tight)",   (3.0,8.0)),
    ("8/15 (loose)",  (8.0,15.0)),
    ("OFF",           (100.0,200.0)),
])

def set_p(p): bt.MIN_PREMIUM_USD = p
sweep("Plancher de prime $/BTC", set_p, [
    ("50$",  50.0), ("150$ (actuel)", 150.0), ("250$", 250.0), ("350$", 350.0),
])

def set_cv(c): bt.SIZE_CONVEXITY = c
sweep("Convexité du sizing", set_cv, [
    ("1.0 (linéaire)", 1.0), ("1.5 (actuel)", 1.5), ("2.0", 2.0),
])

print(f"\n  Rappel : 1 jour de données, régime calme — direction indicative, pas définitif.")
