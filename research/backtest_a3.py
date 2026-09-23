"""
backtest_a3.py — A3 : sensibilité DD aux paramètres gamma / delta / TTE.

Hypothèse : les puts court-terme proches du strike (fort gamma) portent la perte
de gap. Démarrer la pénalité gamma plus tôt, plafonner le delta plus loin OTM, ou
retirer les TTE très courts (3j) réduit la sensibilité au gap.

On mute les globals de backtest.py et on rejoue bt.run (CB binaire, conv1.5) pour
isoler l'effet de chaque paramètre. Métrique : frontière DD / PnL / Calmar.

Usage : python backtest_a3.py [--years 4]
"""
import sys, math, argparse, io, contextlib
sys.path.insert(0, '.')
import backtest as bt

# valeurs d'origine
ORIG = dict(GAMMA_PEN_START=bt.GAMMA_PEN_START, GAMMA_SCORE_CAP=bt.GAMMA_SCORE_CAP,
            DELTA_TARGETS=list(bt.DELTA_TARGETS), TTE_CHOICES=list(bt.TTE_CHOICES))

def stats(ec):
    eq = [e[1] for e in ec]
    rets = [eq[i]-eq[i-1] for i in range(1, len(eq))]
    peak, dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v); dd = max(dd, peak-v)
    m = sum(rets)/len(rets); s = (sum((r-m)**2 for r in rets)/len(rets))**0.5
    sharpe = m/s*math.sqrt(365) if s > 0 else 0
    worst = min(ec[i][2] for i in range(1, len(ec)))
    pnl_an = eq[-1]/len(eq)*365
    return eq[-1], dd, (pnl_an/dd if dd>0 else 0), sharpe, worst

def run_cfg(years, **over):
    for k, v in ORIG.items(): setattr(bt, k, v if not isinstance(v, list) else list(v))
    for k, v in over.items(): setattr(bt, k, v)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ec = bt.run(years, always_one=True, circuit_breaker=True)
    return stats(ec)

CONFIGS = [
    ("BASELINE (g5/10, d≤.25, 3-21j)", {}),
    # pénalité gamma plus précoce
    ("gamma start 4",         dict(GAMMA_PEN_START=4.0)),
    ("gamma start 3",         dict(GAMMA_PEN_START=3.0)),
    ("gamma 3 / cap 8",       dict(GAMMA_PEN_START=3.0, GAMMA_SCORE_CAP=8.0)),
    # plafond delta plus loin OTM (retirer les deltas proches ATM)
    ("delta ≤ .20",           dict(DELTA_TARGETS=[-0.05,-0.08,-0.12,-0.16,-0.20])),
    ("delta ≤ .16",           dict(DELTA_TARGETS=[-0.05,-0.08,-0.12,-0.16])),
    ("delta ≤ .12",           dict(DELTA_TARGETS=[-0.05,-0.08,-0.12])),
    # retirer le TTE 3j (le plus gamma)
    ("TTE 7-21 (sans 3j)",    dict(TTE_CHOICES=[7,14,21])),
    ("TTE 7-21 + delta ≤.16", dict(TTE_CHOICES=[7,14,21], DELTA_TARGETS=[-0.05,-0.08,-0.12,-0.16])),
    # combos
    ("gamma3 + delta ≤.16",   dict(GAMMA_PEN_START=3.0, DELTA_TARGETS=[-0.05,-0.08,-0.12,-0.16])),
    ("gamma3 + TTE7-21 + d.16",dict(GAMMA_PEN_START=3.0, TTE_CHOICES=[7,14,21], DELTA_TARGETS=[-0.05,-0.08,-0.12,-0.16])),
]

if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('--years', type=float, default=4.0)
    a = ap.parse_args()
    print(f"\n  A3 — GAMMA / DELTA / TTE — BTC, conv1.5 + CB binaire, {a.years} ans")
    print(f"  {'Config':<32} {'PnL':>9} {'MaxDD':>8} {'Calmar':>7} {'Sharpe':>7} {'PireJ':>8}")
    print(f"  {'-'*32} {'-'*9} {'-'*8} {'-'*7} {'-'*7} {'-'*8}")
    rows = []
    for label, over in CONFIGS:
        pnl, dd, cal, sh, w = run_cfg(a.years, **over)
        rows.append((label, pnl, dd, cal, sh, w))
        print(f"  {label:<32} {pnl:>8,.0f}$ {dd:>7,.0f}$ {cal:>7.2f} {sh:>7.2f} {w:>7,.0f}$")
    base = rows[0]; best = max(rows[1:], key=lambda r: r[3])
    print(f"\n  Baseline : PnL {base[1]:,.0f}$ · MaxDD {base[2]:,.0f}$ · Calmar {base[3]:.2f}")
    print(f"  Meilleur : {best[0]} · PnL {best[1]:,.0f}$ · MaxDD {best[2]:,.0f}$ · Calmar {best[3]:.2f}")
