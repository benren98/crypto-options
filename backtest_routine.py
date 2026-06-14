"""
backtest_routine.py — Routine automatique de backtests pilotée par le fit de skew actuel.

À chaque exécution :
  1. fitte la surface de skew réelle du jour (par maturité, en mémoire) ;
  2. rejoue la batterie standard de sweeps de paramètres SOUS ce skew réel
     (poids du score, SKEW_NORM, seuil d'entrée, MIN_PREMIUM, pénalité gamma, convexité) ;
  3. classe chaque paramètre par SENSIBILITÉ (amplitude du Calmar) → « à regarder » ;
  4. écrit backtest_routine.json (consommé par le dashboard backtests).

Pensé pour tourner 1×/semaine via GitHub Actions. Plus besoin de re-décrire quoi tester.

Usage : python backtest_routine.py [--years 4]
"""
import sys, io, contextlib, math, json, argparse
from datetime import datetime, timezone
sys.path.insert(0, '.')
import backtest as bt
import fit_vol_model as fm

OUT_FILE = "backtest_routine.json"

# Valeurs de production (référence des sweeps)
PROD = dict(W_IVHV=0.30, W_YIELD=0.25, W_SKEW=0.45, SKEW_NORM=0.20,
            ENTRY=0.50, PREMIUM=150.0, GPEN=5.0, GCAP=10.0, CONVEX=1.5)


def _stats(ec):
    eq = [e[1] for e in ec]
    peak, dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v); dd = max(dd, peak - v)
    rets = [eq[i]-eq[i-1] for i in range(1, len(eq))]
    m = sum(rets)/len(rets); s = (sum((r-m)**2 for r in rets)/len(rets))**0.5
    sharpe = m/s*math.sqrt(365) if s > 0 else 0
    worst = min(ec[i][2] for i in range(1, len(ec)))
    cal = eq[-1]/len(eq)*365/dd if dd > 0 else 0
    return dict(pnl=round(eq[-1]), maxdd=round(dd), calmar=round(cal, 2),
                sharpe=round(sharpe, 2), worst=round(worst))


def _apply(cfg):
    bt.SCORE_W_IVHV, bt.SCORE_W_YIELD, bt.SCORE_W_SKEW = cfg['W_IVHV'], cfg['W_YIELD'], cfg['W_SKEW']
    bt.SKEW_NORM = cfg['SKEW_NORM']; bt.ENTRY_SCORE_MIN = cfg['ENTRY']
    bt.MIN_PREMIUM_USD = cfg['PREMIUM']; bt.GAMMA_PEN_START = cfg['GPEN']
    bt.GAMMA_SCORE_CAP = cfg['GCAP']; bt.SIZE_CONVEXITY = cfg['CONVEX']


def _run(years, cfg, want_curve=False):
    _apply(cfg)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ec = bt.run(years, circuit_breaker=True)
    out = buf.getvalue()
    ntr = [l for l in out.splitlines() if "Trades" in l]
    n = int(ntr[0].split(":")[1].split("|")[0].strip()) if ntr else None
    st = _stats(ec); st['trades'] = n
    if want_curve:
        st['curve'] = [[str(e[0]), round(e[1])] for e in ec]
    return st


def run(years=4.0):
    # 1. Fit surface du jour (en mémoire, dès 1 jour)
    surf = fm.fit_surface(min_snapshots=1)
    if surf:
        bt.SKEW_SURFACE = surf["buckets"]
        bt.SKEW_A, bt.SKEW_B = surf["pooled"]["a"], surf["pooled"]["b"]
        print(f"  Skew fité : {surf['n_snapshots']}j, {len(surf['buckets'])} buckets")
    else:
        print("  Pas de surface réelle — sweeps sous skew linéaire 0.013")

    base = _run(years, PROD, want_curve=True)
    print(f"\n  Baseline (prod) : PnL {base['pnl']:,}$  MaxDD {base['maxdd']:,}$  Calmar {base['calmar']}\n")

    # 2. Batterie de sweeps (un paramètre varie, les autres = prod)
    def sweep(name, key, values, fmt=str):
        results = []
        for v in values:
            cfg = dict(PROD)
            if key == 'WEIGHTS':
                cfg['W_IVHV'], cfg['W_YIELD'], cfg['W_SKEW'] = v
                label = f"{v[0]}/{v[1]}/{v[2]}"
            else:
                cfg[key] = v; label = fmt(v)
            st = _run(years, cfg)
            st['label'] = label
            results.append(st)
        cals = [r['calmar'] for r in results]
        sens = round(max(cals) - min(cals), 2)
        best = max(results, key=lambda r: r['calmar'])
        return dict(param=name, results=results, sensitivity=sens, best_label=best['label'],
                    best_calmar=best['calmar'])

    sweeps = [
        sweep("Poids score (ivhv/yield/skew)", 'WEIGHTS',
              [(0.40,0.30,0.30),(0.30,0.25,0.45),(0.25,0.20,0.55),(0.20,0.15,0.65)]),
        sweep("SKEW_NORM", 'SKEW_NORM', [0.20,0.40,0.60,0.80], lambda v: f"{v:.2f}"),
        sweep("Seuil d'entrée", 'ENTRY', [0.45,0.50,0.55,0.60,0.65,0.70], lambda v: f"{v:.2f}"),
        sweep("Plancher prime $", 'PREMIUM', [50,150,250,350], lambda v: f"{int(v)}$"),
        sweep("Pénalité gamma (start)", 'GPEN', [3.0,5.0,8.0,100.0], lambda v: "OFF" if v>=100 else f"{int(v)}"),
        sweep("Convexité sizing", 'CONVEX', [1.0,1.5,2.0], lambda v: f"{v:.1f}"),
    ]

    # 3. Classement par sensibilité
    ranked = sorted(sweeps, key=lambda s: -s['sensitivity'])
    print("  ── Paramètres classés par SENSIBILITÉ (Calmar) — à regarder en priorité ──")
    for s in ranked:
        flag = " ⚠" if s['sensitivity'] >= 1.0 else ""
        print(f"  {s['param']:<32} ΔCalmar {s['sensitivity']:>5}  → meilleur {s['best_label']} "
              f"(Calmar {s['best_calmar']}){flag}")

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "years": years,
        "skew_fit": surf,
        "prod_config": PROD,
        "baseline": base,
        "sweeps": ranked,
    }
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\n  → {OUT_FILE} écrit ({len(sweeps)} sweeps).")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument('--years', type=float, default=4.0)
    run(ap.parse_args().years)
