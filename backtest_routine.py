"""
backtest_routine.py — Routine automatique de backtests pilotée par le fit de skew actuel.

À chaque exécution :
  1. fitte la surface de skew réelle (par maturité + régime DVOL, en mémoire) ;
  2. rejoue une batterie COMPLÈTE de sweeps de paramètres SOUS ce skew réel :
     scoring (poids, normalisations IV/HV & skew, horizon HV 5/10/30j), entrée,
     plancher de prime, pénalité gamma, sizing (convexité, cap, plancher de rang),
     et tous les paramètres du circuit breaker (paliers, seuils, keep, reprise) ;
  3. classe chaque paramètre par SENSIBILITÉ (amplitude du Calmar) ;
  4. marque la config ACTUELLE et la meilleure ; écrit backtest_routine.json.

Tourne 1×/semaine via GitHub Actions. Usage : python backtest_routine.py [--years 4]
"""
import sys, io, contextlib, math, json, argparse
from datetime import datetime, timezone
sys.path.insert(0, '.')
import backtest as bt
import fit_vol_model as fm

OUT_FILE = "backtest_routine.json"

# Config de PRODUCTION actuelle (référence des sweeps + ligne mise en avant)
PROD = dict(
    W_IVHV=0.30, W_YIELD=0.25, W_SKEW=0.45,
    SKEW_NORM=0.20, IVHV_NORM=1.0,
    HV5=0.0, HV10=0.5, HV30=0.5,
    ENTRY=0.50, PREMIUM=150.0, GPEN=5.0, GCAP=10.0,
    CONVEX=1.5, MAXBTC=5.0, RANKFLOOR=0.5,
    CB_T2M=10.0, CB_T2D=12.0, CB_T1M1=5.0, CB_T1M3=6.0, CB_T1K=0.30, CB_T1R=3.0,
)


def _apply(c):
    bt.SCORE_W_IVHV, bt.SCORE_W_YIELD, bt.SCORE_W_SKEW = c['W_IVHV'], c['W_YIELD'], c['W_SKEW']
    bt.SKEW_NORM, bt.IVHV_NORM = c['SKEW_NORM'], c['IVHV_NORM']
    bt.HV_W5, bt.HV_W10, bt.HV_W30 = c['HV5'], c['HV10'], c['HV30']
    bt.ENTRY_SCORE_MIN, bt.MIN_PREMIUM_USD = c['ENTRY'], c['PREMIUM']
    bt.GAMMA_PEN_START, bt.GAMMA_SCORE_CAP = c['GPEN'], c['GCAP']
    bt.SIZE_CONVEXITY, bt.MAX_PORTFOLIO_BTC, bt.RANK_FLOOR = c['CONVEX'], c['MAXBTC'], c['RANKFLOOR']
    bt.CB_MOVE_3D_PCT, bt.CB_DVOL_3D_PTS = c['CB_T2M'], c['CB_T2D']
    bt.CB_T1_MOVE_1D, bt.CB_T1_MOVE_3D = c['CB_T1M1'], c['CB_T1M3']
    bt.CB_T1_KEEP, bt.CB_T1_RESTORE = c['CB_T1K'], c['CB_T1R']


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


def _run(years, cfg, want_curve=False):
    _apply(cfg)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ec = bt.run(years, circuit_breaker=True)
    ntr = [l for l in buf.getvalue().splitlines() if "Trades" in l]
    st = _stats(ec)
    st['trades'] = int(ntr[0].split(":")[1].split("|")[0].strip()) if ntr else None
    if want_curve:
        st['curve'] = [[str(e[0]), round(e[1])] for e in ec]
    return st


def run(years=4.0):
    surf = fm.fit_surface(min_snapshots=1)
    if surf:
        bt.SKEW_SURFACE = surf["buckets"]; bt.SKEW_POOLED = surf["pooled"]
        n_reg = sum(1 for bk in surf["buckets"] if bk.get("regime_aware"))
        print(f"  Skew fité : {surf['n_snapshots']}j, {len(surf['buckets'])} buckets ({n_reg} régime-aware)")
    else:
        print("  Pas de surface réelle — sweeps sous skew linéaire 0.013")

    base = _run(years, PROD, want_curve=True)
    print(f"\n  Config ACTUELLE : PnL {base['pnl']:,}$  MaxDD {base['maxdd']:,}$  Calmar {base['calmar']}\n")

    def _cur(key, v):
        """v correspond-il à la valeur de production ?"""
        if key == 'WEIGHTS':
            return abs(v[0]-PROD['W_IVHV'])<1e-6 and abs(v[1]-PROD['W_YIELD'])<1e-6 and abs(v[2]-PROD['W_SKEW'])<1e-6
        if key == 'HV':
            return abs(v[0]-PROD['HV5'])<1e-6 and abs(v[1]-PROD['HV10'])<1e-6 and abs(v[2]-PROD['HV30'])<1e-6
        return abs(v-PROD[key])<1e-9

    def sweep(name, key, values, fmt=str):
        results = []
        for v in values:
            cfg = dict(PROD)
            if key == 'WEIGHTS':
                cfg['W_IVHV'], cfg['W_YIELD'], cfg['W_SKEW'] = v; label = f"{v[0]}/{v[1]}/{v[2]}"
            elif key == 'HV':
                cfg['HV5'], cfg['HV10'], cfg['HV30'] = v; label = fmt(v)
            else:
                cfg[key] = v; label = fmt(v)
            st = _run(years, cfg)
            st['label'] = label; st['is_current'] = _cur(key, v)
            results.append(st)
        cals = [r['calmar'] for r in results]
        best = max(results, key=lambda r: r['calmar'])
        for r in results:
            r['is_best'] = (r is best)
        return dict(param=name, results=results, sensitivity=round(max(cals)-min(cals), 2),
                    best_label=best['label'], best_calmar=best['calmar'])

    sweeps = [
        sweep("Poids score (ivhv/yield/skew)", 'WEIGHTS',
              [(0.40,0.30,0.30),(0.35,0.30,0.35),(0.30,0.25,0.45),(0.30,0.20,0.50),
               (0.25,0.20,0.55),(0.20,0.15,0.65),(0.50,0.25,0.25),(0.20,0.40,0.40)]),
        sweep("SKEW_NORM", 'SKEW_NORM', [0.15,0.20,0.30,0.40,0.50,0.60,0.80], lambda v:f"{v:.2f}"),
        sweep("IV/HV — normalisation", 'IVHV_NORM', [0.5,0.75,1.0,1.5,2.0], lambda v:f"{v:.2f}"),
        sweep("IV/HV — horizon HV (5/10/30j)", 'HV',
              [(0,0,1.0),(0,1.0,0),(1.0,0,0),(0,0.5,0.5),(0.5,0.5,0),(0.34,0.33,0.33),(0,0.7,0.3)],
              lambda v:{(0,0,1.0):"30j",(0,1.0,0):"10j",(1.0,0,0):"5j",(0,0.5,0.5):"10/30",
                        (0.5,0.5,0):"5/10",(0.34,0.33,0.33):"5/10/30",(0,0.7,0.3):"10>30"}.get(tuple(v),str(v))),
        sweep("Seuil d'entrée", 'ENTRY', [0.40,0.45,0.50,0.55,0.60,0.65,0.70,0.75], lambda v:f"{v:.2f}"),
        sweep("Plancher prime $", 'PREMIUM', [50,100,150,200,250,300,400], lambda v:f"{int(v)}$"),
        sweep("Pénalité gamma (start)", 'GPEN', [3.0,4.0,5.0,6.0,8.0,100.0], lambda v:"OFF" if v>=100 else f"{int(v)}"),
        sweep("Sizing — convexité", 'CONVEX', [1.0,1.25,1.5,1.75,2.0], lambda v:f"{v:.2f}"),
        sweep("Sizing — cap notionnel BTC", 'MAXBTC', [3.0,4.0,5.0,6.0,7.0], lambda v:f"{v:.0f}"),
        sweep("Sizing — plancher rang DVOL", 'RANKFLOOR', [0.3,0.4,0.5,0.6,0.7,1.0], lambda v:f"{v:.2f}"),
        sweep("CB — fermeture move 3j %", 'CB_T2M', [8.0,10.0,12.0,15.0,100.0], lambda v:"OFF" if v>=100 else f"−{int(v)}%"),
        sweep("CB — fermeture DVOL 3j pts", 'CB_T2D', [8.0,10.0,12.0,15.0,100.0], lambda v:"OFF" if v>=100 else f"+{int(v)}"),
        sweep("CB — allègement move 1j %", 'CB_T1M1', [4.0,5.0,6.0,7.0,100.0], lambda v:"OFF" if v>=100 else f"−{int(v)}%"),
        sweep("CB — allègement move 3j %", 'CB_T1M3', [5.0,6.0,7.0,8.0,100.0], lambda v:"OFF" if v>=100 else f"−{int(v)}%"),
        sweep("CB — allègement keep", 'CB_T1K', [0.2,0.3,0.4,0.5,1.0], lambda v:"OFF" if v>=1 else f"{v:.0%}"),
        sweep("CB — reprise move 3j %", 'CB_T1R', [2.0,3.0,4.0,5.0], lambda v:f"{v:.0f}%"),
    ]

    ranked = sorted(sweeps, key=lambda s: -s['sensitivity'])
    print("  ── Paramètres classés par SENSIBILITÉ (ΔCalmar) — à regarder en priorité ──")
    for s in ranked:
        flag = " ⚠" if s['sensitivity'] >= 1.0 else ""
        print(f"  {s['param']:<34} ΔCalmar {s['sensitivity']:>5}  → meilleur {s['best_label']} "
              f"(Calmar {s['best_calmar']}){flag}")

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "years": years, "skew_fit": surf, "prod_config": PROD,
        "baseline": base, "sweeps": ranked,
    }
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\n  → {OUT_FILE} écrit ({len(sweeps)} sweeps).")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument('--years', type=float, default=4.0)
    run(ap.parse_args().years)
