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


SPLIT = 0.60   # train (in-sample) = 60% initiaux ; test (out-of-sample) = 40% restants

def _calmar_slice(eq, lo, hi):
    """Calmar sur la fenêtre [lo:hi] de eq=[(date,equity),...]."""
    sub = eq[lo:hi]
    if len(sub) < 30:
        return None
    pnl = sub[-1][1] - sub[0][1]
    peak, dd = sub[0][1], 0.0
    for _, v in sub:
        peak = max(peak, v); dd = max(dd, peak - v)
    return round(pnl/len(sub)*365/dd, 2) if dd > 0 else 0.0


def _windowed(ec):
    """Calmar in-sample / out-of-sample + par année (anti-overfitting)."""
    eq = [(e[0], e[1]) for e in ec]
    n = len(eq); split = int(n*SPLIT)
    by_year = {}
    for i, (d, _) in enumerate(eq):
        by_year.setdefault(d.year, [i, i])[1] = i  # garde (premier, dernier) index
    yearly = {str(y): _calmar_slice(eq, lo, hi+1) for y, (lo, hi) in by_year.items()}
    yvals = [v for v in yearly.values() if v is not None]
    return dict(calmar_is=_calmar_slice(eq, 0, split),
                calmar_oos=_calmar_slice(eq, split, n),
                calmar_min_year=round(min(yvals), 2) if yvals else None,
                yearly=yearly)


def _run(years, cfg, want_curve=False):
    _apply(cfg)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ec = bt.run(years, circuit_breaker=True)
    ntr = [l for l in buf.getvalue().splitlines() if "Trades" in l]
    st = _stats(ec)
    st.update(_windowed(ec))
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
        def _argmax(metric):
            cand = [(i, r.get(metric)) for i, r in enumerate(results) if r.get(metric) is not None]
            return max(cand, key=lambda t: t[1])[0] if cand else None
        bi_is, bi_oos = _argmax('calmar_is'), _argmax('calmar_oos')
        # Recommandation = l'optimum OUT-OF-SAMPLE (ce qui généralise) ; à défaut le full
        bi = bi_oos if bi_oos is not None else max(range(len(results)), key=lambda i: results[i]['calmar'])
        best = results[bi]
        for i, r in enumerate(results):
            r['is_best'] = (i == bi)
            r['is_oos_best'] = (i == bi_oos)
            r['is_is_best'] = (i == bi_is)
        # Test d'overfit n°1 : l'optimum in-sample == l'optimum out-of-sample ?
        is_oos_agree = (bi_is is not None and bi_is == bi_oos)
        # Plateau : voisins de l'optimum OOS aussi bons (≥80%) ?
        neigh = [results[i].get('calmar_oos') for i in (bi-1, bi+1) if 0 <= i < len(results)]
        neigh = [c for c in neigh if c is not None]
        plateau = bool(neigh) and best.get('calmar_oos') and all(c >= 0.8*best['calmar_oos'] for c in neigh)
        robust = is_oos_agree and plateau and (best.get('calmar_min_year') or 0) > 0
        return dict(param=name, results=results, sensitivity=round(max(cals)-min(cals), 2),
                    best_label=best['label'], best_calmar=best['calmar'],
                    is_opt_label=(results[bi_is]['label'] if bi_is is not None else None),
                    best_is=best.get('calmar_is'), best_oos=best.get('calmar_oos'),
                    best_min_year=best.get('calmar_min_year'),
                    is_oos_agree=is_oos_agree, plateau=plateau, robust=robust)

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
    print(f"  ── Sweeps · test d'overfit : optimum IS ({int(SPLIT*100)}%) vs OOS ({100-int(SPLIT*100)}%) ──")
    print(f"  {'Paramètre':<32} {'Δ':>5} {'opt.OOS':>12} {'opt.IS':>12} {'OOS_cal':>7} {'min/an':>6}  verdict")
    for s in ranked:
        def g(x): return "—" if x is None else f"{x}"
        if s['sensitivity'] < 0.5:      verdict = "· peu sensible"
        elif s['robust']:               verdict = "✅ robuste (IS=OOS, plateau)"
        elif not s['is_oos_agree']:     verdict = "⛔ overfit (opt. IS ≠ OOS)"
        elif not s['plateau']:          verdict = "⚠ pic isolé"
        else:                           verdict = "⚠ une année porte tout"
        same = "=" if s['is_oos_agree'] else "≠"
        print(f"  {s['param']:<32} {s['sensitivity']:>5} {s['best_label']:>12} "
              f"{(s['is_opt_label'] or '—'):>11}{same} {g(s['best_oos']):>7} {g(s['best_min_year']):>6}  {verdict}")

    actionable = [f"{s['param']}→{s['best_label']}" for s in ranked if s['robust'] and s['sensitivity'] >= 1.0]
    print(f"\n  → À ajuster en priorité (optimum stable IS=OOS, plateau, toutes années, sensible) :")
    print(f"     {chr(10)+'     '.join(actionable) if actionable else 'aucun — données insuffisantes / pas de gain robuste'}")
    print(f"  Rappel anti-overfit : changer 1-2 params à la fois, confirmer sur ETH, ne pas empiler les optima.")

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
