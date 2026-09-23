"""
backtest_routine.py — Routine automatique de backtests (hebdo, GitHub Actions).

À chaque exécution :
  1. fitte la surface de skew réelle (par maturité + régime DVOL, en mémoire) ;
  2. rejoue la config de PRODUCTION puis une batterie de sweeps, famille par famille :
     scoring, entrée, sizing, HEDGE (ratio, cadence, contrôle horaire/journalier, bande,
     mise à plat du résiduel), circuit breaker ;
  3. stresse les HYPOTHÈSES du modèle (frais Deribit, spread bid/ask, funding) : elles
     mesurent la fragilité du résultat, elles ne sont jamais « recommandées » ;
  4. juge chaque paramètre sur 5 folds contigus (≈ régimes) : pire fold (maximin),
     accord du vainqueur entre folds, plateau → verdict ✅ robuste / ⛔ / ⚠ ;
  5. écrit backtest_routine.json (lu par generate_backtest_html.py et le dashboard v2).

Source unique des valeurs de prod : les constantes de backtest.py (elles-mêmes miroir
du live, vérifié par check_params_sync.py). Aucune copie à tenir à jour ici.

Usage : python backtest_routine.py [--years 4]
"""
import sys, io, contextlib, json, argparse, statistics
from datetime import datetime, timezone
sys.path.insert(0, '.')
import backtest as bt
import fit_vol_model as fm

OUT_FILE = "backtest_routine.json"
NFOLDS   = 5      # folds contigus (~10 mois chacun sur 4 ans) → autant de sous-régimes
MIN_GAIN = 1.0    # gain minimal de Calmar moyen (folds) pour recommander un changement
MIN_SENSITIVITY = 0.5  # amplitude minimale du Calmar complet pour qu'un paramètre soit « actif »
DD_FLOOR_FRAC = 0.25   # plancher de drawdown d'un fold = 25 % du MaxDD de la prod (Calmar borné)


def f2(v):  return f"{v:.2f}"
def f0(v):  return f"{v:.0f}"
def pct0(v): return f"{v:.0%}"
def off_if(th, fmt):
    return lambda v: "OFF" if (v >= th if th > 0 else v == 0) else fmt(v)


# (famille, libellé, attribut(s) de backtest.py, valeurs, format, type)
SWEEPS = [
    ("Scoring", "Poids score (ivhv/yield/skew)", ("SCORE_W_IVHV", "SCORE_W_YIELD", "SCORE_W_SKEW"),
     [(0.40, 0.30, 0.30), (0.35, 0.30, 0.35), (0.30, 0.25, 0.45), (0.30, 0.20, 0.50),
      (0.25, 0.20, 0.55), (0.20, 0.15, 0.65), (0.50, 0.25, 0.25), (0.20, 0.40, 0.40)],
     lambda v: "/".join(f"{x:g}" for x in v), "param"),
    ("Scoring", "Skew — normalisation", "SKEW_NORM", [0.15, 0.20, 0.30, 0.40, 0.50, 0.60, 0.80, 1.0, 1.2], f2, "param"),
    ("Scoring", "IV/HV — normalisation", "IVHV_NORM", [0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0], f2, "param"),
    ("Scoring", "Yield — normalisation", "YIELD_NORM", [0.15, 0.20, 0.30, 0.40, 0.50, 0.60], f2, "param"),
    ("Scoring", "IV/HV — horizon HV (5/10/30 j)", ("HV_W5", "HV_W10", "HV_W30"),
     [(0, 0, 1.0), (0, 1.0, 0), (1.0, 0, 0), (0, 0.5, 0.5), (0.5, 0.5, 0), (0.34, 0.33, 0.33), (0, 0.7, 0.3)],
     lambda v: {(0, 0, 1.0): "30j", (0, 1.0, 0): "10j", (1.0, 0, 0): "5j", (0, 0.5, 0.5): "10/30",
                (0.5, 0.5, 0): "5/10", (0.34, 0.33, 0.33): "5/10/30", (0, 0.7, 0.3): "10>30"}.get(tuple(v), str(v)),
     "param"),

    ("Entrée", "Seuil d'entrée (score)", "ENTRY_SCORE_MIN", [0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.70], f2, "param"),
    ("Entrée", "Plancher de prime ($/BTC)", "MIN_PREMIUM_USD", [50, 100, 150, 200, 250, 300, 400], lambda v: f"{v:.0f}$", "param"),
    ("Entrée", "Porte DVOL min", "DVOL_MIN", [32.0, 35.0, 38.0, 40.0, 42.0], f0, "param"),
    ("Entrée", "Delta max (proximité ATM)", "SCAN_DELTA_MIN", [-0.08, -0.10, -0.12, -0.16, -0.20, -0.30], f2, "param"),
    ("Entrée", "Fenêtre d'échéances scannée (j)", ("SCAN_TTE_MIN", "SCAN_TTE_MAX"),
     [(1.0, 30.0), (1.0, 14.0), (1.0, 8.0), (3.0, 30.0), (7.0, 30.0), (14.0, 30.0), (3.0, 14.0)],
     lambda v: f"{v[0]:g}–{v[1]:g} j", "param"),
    ("Entrée", "Nouvelles positions max / jour", "MAX_ENTRIES_PER_DAY", [1, 2, 3, 4, 0],
     lambda v: "illimité" if v == 0 else str(v), "param"),
    ("Entrée", "Ré-entrée — boost de score", "ENTRY_SCORE_REENTRY_BOOST", [0.0, 0.03, 0.05, 0.08, 0.10, 0.15],
     lambda v: f"+{v:.2f}", "param"),
    ("Entrée", "Espacement delta (même échéance)", "DELTA_MIN_SPACING", [0.0, 0.04, 0.06, 0.08, 0.12], f2, "param"),
    ("Entrée", "Pénalité gamma (début, pts)", "GAMMA_PEN_START", [3.0, 4.0, 5.0, 6.0, 100.0], off_if(100, f0), "param"),
    ("Entrée", "Gamma — cap dur (pts)", "GAMMA_ENTRY_CAP", [0.0, 3.0, 4.0, 5.0], off_if(0, lambda v: f"{v:.1f}"), "candidate"),

    ("Sizing", "Convexité (score^x)", "SIZE_CONVEXITY", [1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0], f2, "param"),
    ("Sizing", "Cap notionnel (BTC)", "MAX_PORTFOLIO_BTC", [3.0, 4.0, 5.0, 6.0, 7.0], f0, "param"),
    ("Sizing", "Plancher rang DVOL", "RANK_FLOOR", [0.1, 0.3, 0.5, 0.6, 0.7, 0.85, 1.0], f2, "param"),

    ("Hedge", "Hedge — ratio couvert", "HEDGE_RATIO", [0.0, 0.5, 0.7, 0.85, 1.0], pct0, "param"),
    # Politique hybride : au plus un rebalancement « normal » toutes les N h (si la bande est dépassée),
    # mais contrôle à chaque run horaire : rehedge immédiat si le book change et/ou si la dérive
    # dépasse une bande d'urgence = MULT × bande normale.
    ("Hedge", "Hedge — politique (cadence · rehedge si le book change · bande d'urgence)",
     ("HEDGE_EVERY_H", "HEDGE_CADENCE_EXEMPT", "HEDGE_URGENT_MULT"),
     [(1, True, 0.0), (4, True, 0.0), (8, True, 0.0), (24, False, 0.0), (24, True, 0.0), (24, True, 1.5),
      (24, True, 2.0), (24, True, 3.0), (24, False, 3.0), (8, True, 2.0)],
     lambda v: "1 h (live)" if v[0] == 1 else
     f"{v[0]} h" + (" + book" if v[1] else " strict") + (f" + urgence ×{v[2]:g}" if v[2] else ""), "param"),
    ("Hedge", "Hedge — bande de base (%)", "HEDGE_THRESHOLD_BASE_PCT", [2.0, 3.0, 5.0, 7.0, 10.0], f0, "param"),
    ("Hedge", "Hedge — bande", "HEDGE_THRESHOLD_MODE", ["absolute", "notional"],
     lambda v: {"absolute": "BTC fixe (live)", "notional": "× notionnel"}[v], "param"),
    ("Hedge", "Hedge — mise à plat du résiduel (BTC)", "HEDGE_FLATTEN_DELTA", [0.0, 0.01, 0.02, 0.05],
     off_if(0, lambda v: f"<{v:.2f}"), "param"),

    ("Circuit breaker", "Fermeture — move 3 j (%)", "CB_MOVE_3D_PCT", [8.0, 10.0, 12.0, 15.0, 100.0], off_if(100, lambda v: f"−{v:.0f}%"), "param"),
    ("Circuit breaker", "Fermeture — DVOL 3 j (pts)", "CB_DVOL_3D_PTS", [8.0, 10.0, 12.0, 15.0, 100.0], off_if(100, lambda v: f"+{v:.0f}"), "param"),
    ("Circuit breaker", "Re-entrée après fermeture — |move 3 j| (%)", "CB_REENTRY_MOVE", [2.0, 3.0, 4.0, 6.0, 8.0], lambda v: f"{v:.0f}%", "param"),
    ("Circuit breaker", "Allègement — move 1 j (%)", "CB_T1_MOVE_1D", [4.0, 5.0, 6.0, 7.0, 100.0], off_if(100, lambda v: f"−{v:.0f}%"), "param"),
    ("Circuit breaker", "Allègement — move 3 j (%)", "CB_T1_MOVE_3D", [5.0, 6.0, 7.0, 8.0, 100.0], off_if(100, lambda v: f"−{v:.0f}%"), "param"),
    ("Circuit breaker", "Allègement — part conservée", "CB_T1_KEEP", [0.0, 0.2, 0.3, 0.4, 0.5, 0.7], pct0, "param"),
    ("Circuit breaker", "Allègement gradué (palier 1)", "GRADUATED_CB", [True, False],
     lambda v: "ON" if v else "OFF (fermeture seule)", "param"),
    ("Circuit breaker", "Allègement — seuils 1 j / 3 j (%)", ("CB_T1_MOVE_1D", "CB_T1_MOVE_3D"),
     [(4.0, 5.0), (5.0, 6.0), (6.0, 7.0), (7.0, 8.0), (8.0, 10.0), (10.0, 12.0)],
     lambda v: f"−{v[0]:g} % / −{v[1]:g} %", "param"),
    ("Circuit breaker", "Allègement — reprise |move 3 j| (%)", "CB_T1_RESTORE", [2.0, 3.0, 4.0, 5.0], lambda v: f"{v:.0f}%", "param"),
    ("Circuit breaker", "Allègement — cooldown après reprise (j)", "CB_T1_COOLDOWN_D", [0, 1, 2, 3, 5], off_if(0, lambda v: f"{v:.0f}j"), "param"),
    ("Circuit breaker", "Allègement — reprise × cooldown", ("CB_T1_RESTORE", "CB_T1_COOLDOWN_D"),
     [(3.0, 0), (3.0, 2), (4.0, 1), (4.0, 2), (5.0, 2), (2.0, 3)],
     lambda v: f"|3 j| < {v[0]:g} % · {v[1]:g} j", "param"),

    ("Hypothèses", "Frais Deribit (× grille)", "FEE_MULT", [0.0, 0.5, 1.0, 1.5, 2.0], lambda v: f"×{v:g}", "assumption"),
    ("Hypothèses", "Demi-spread bid/ask (pts de vol, DVOL ≤ 40)", "BA_HAIRCUT_VOLPTS", [0.5, 0.7, 1.0, 1.5, 2.5],
     lambda v: f"{v:g} pt", "assumption"),
    ("Hypothèses", "Échéances disponibles", "EXPIRY_CALENDAR", ["deribit", "fixed"],
     lambda v: "calendrier Deribit (live)" if v == "deribit" else "3/7/14/21 j (ancien modèle)", "assumption"),
    ("Hypothèses", "Funding du hedge", "USE_REAL_FUNDING", [True, False], lambda v: "réel" if v else "forfait payé (0,01 %/j)", "assumption"),
    ("Hypothèses", "Contrôle intraday (hedge + circuit breaker)", "HEDGE_INTRADAY", [True, False],
     lambda v: "horaire (live)" if v else "clôture seule", "assumption"),
    ("Hypothèses", "Cadence effective du process (h)", "RUN_EVERY_H", [1, 2, 3, 6],
     lambda v: f"toutes les {v} h", "assumption"),
]

ATTRS = sorted({a for s in SWEEPS for a in (s[2] if isinstance(s[2], tuple) else (s[2],))})
PROD = {a: getattr(bt, a) for a in ATTRS}   # config de production = valeurs du module


def _apply(cfg):
    for a, v in cfg.items():
        setattr(bt, a, list(v) if isinstance(v, list) else v)


def _as_cfg(attrs, value):
    if isinstance(attrs, tuple):
        return dict(zip(attrs, value))
    return {attrs: value}


def _is_current(attrs, value):
    cfg = _as_cfg(attrs, value)
    for a, v in cfg.items():
        p = PROD[a]
        if isinstance(v, (int, float)) and not isinstance(v, bool) and isinstance(p, (int, float)) and not isinstance(p, bool):
            if abs(v - p) > 1e-9:
                return False
        elif list(v) != list(p) if isinstance(v, (list, tuple)) else v != p:
            return False
    return True


def _stats(ec):
    eq = [e[1] for e in ec]
    peak, dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v); dd = max(dd, peak - v)
    rets = [eq[i] - eq[i - 1] for i in range(1, len(eq))]
    m = sum(rets) / len(rets); s = (sum((r - m) ** 2 for r in rets) / len(rets)) ** 0.5
    return dict(pnl=round(eq[-1]), maxdd=round(dd),
                calmar=round(eq[-1] / len(eq) * 365 / dd, 2) if dd > 0 else 0,
                sharpe=round(m / s * 365 ** 0.5, 2) if s > 0 else 0,
                worst=round(min(ec[i][2] for i in range(1, len(ec)))))


def _calmar_slice(eq, lo, hi, dd_floor):
    """Calmar d'un fold avec plancher de drawdown : un fold calme (DD ≈ 0) ne produit plus
    de Calmar démesuré qui écrase la moyenne (défaut des versions précédentes)."""
    sub = eq[lo:hi]
    if len(sub) < 30:
        return None
    pnl = sub[-1][1] - sub[0][1]
    peak, dd = sub[0][1], 0.0
    for _, v in sub:
        peak = max(peak, v); dd = max(dd, peak - v)
    return round(pnl / len(sub) * 365 / max(dd, dd_floor), 2)


def _folds(ec, dd_floor):
    eq = [(e[0], e[1]) for e in ec]
    n = len(eq)
    bnd = [round(n * i / NFOLDS) for i in range(NFOLDS + 1)]
    cals = [_calmar_slice(eq, bnd[i], bnd[i + 1], dd_floor) for i in range(NFOLDS)]
    valid = [c for c in cals if c is not None]
    return dict(folds=cals,
                fold_dates=[str(eq[bnd[i]][0]) for i in range(NFOLDS)],
                worst_fold=round(min(valid), 2) if valid else None,
                mean_fold=round(sum(valid) / len(valid), 2) if valid else None,
                median_fold=round(statistics.median(valid), 2) if valid else None)


def _run(years, cfg, dd_floor, want_curve=False):
    _apply(cfg)
    bt.TRACK_PM = want_curve          # portfolio margin estimée seulement pour la config de prod
    with contextlib.redirect_stdout(io.StringIO()):
        ec = bt.run(years, circuit_breaker=True)
    bt.TRACK_PM = False
    _apply(PROD)
    st = _stats(ec)
    st.update(_folds(ec, dd_floor))
    L = bt._LAST_RUN
    fees = sum(L["fees"].values())
    opt = L["attrib"]["options"]
    st.update(trades=L["trades"], rebalances=L["rebalances"], fees=round(fees),
              fees_detail={k: round(v) for k, v in L["fees"].items()},
              hedge=round(L["attrib"]["hedge"]), options=round(opt), funding=round(L["funding"]),
              kept_pct=round((opt + L["attrib"]["hedge"] + L["funding"] - fees) / opt * 100, 1) if opt > 0 else None,
              capital=L.get("capital"))
    sm = (L.get("capital") or {}).get("sm") or {}
    st["capital_sm"] = sm.get("capital_usd")                  # capital requis en marge standard
    st["roc_sm"] = (sm.get("roc_pct") or {}).get("idle")      # rendement annuel sur ce capital (cash dormant)
    if want_curve:
        st["curve"] = [[str(e[0]), round(e[1])] for e in ec]
    return st


def _is_ordinal(attrs, values):
    """Sweep ordonné (valeurs numériques d'un seul attribut) : le voisinage a un sens (plateau,
    extension de grille). Combinaisons, listes et catégories : non."""
    return isinstance(attrs, str) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values)


def _judge(name, family, kind, results, ordinal=True):
    """Verdict anti-overfit d'un sweep (maximin + accord entre folds + plateau).
    kind : "param" (levier live, recommandable) · "candidate" (levier sans code live : affiché,
    jamais recommandé) · "assumption" (hypothèse de modélisation stressée)."""
    cals = [r["calmar"] for r in results]
    cur_i = next((i for i, r in enumerate(results) if r.get("is_current")), None)
    fold_winner = []
    for fi in range(NFOLDS):
        cand = [(i, results[i]["folds"][fi]) for i in range(len(results)) if results[i]["folds"][fi] is not None]
        if cand:
            top = max(c for _, c in cand)
            tied = [i for i, c in cand if abs(c - top) < 0.005]
            # Ex-aequo : la victoire va à la config actuelle si elle en fait partie, sinon à personne
            # (avant : au premier de la liste → « robustes » artificiels sur des valeurs inertes).
            fold_winner.append(tied[0] if len(tied) == 1 else (cur_i if cur_i in tied else None))
    pos = [r for r in results if (r.get("worst_fold") or -1e9) > 0]
    pool = pos if pos else results
    best = max(pool, key=lambda r: r.get("mean_fold") if r.get("mean_fold") is not None else -1e9)
    bi = results.index(best)
    for i, r in enumerate(results):
        r["is_best"] = (i == bi)
    wins = fold_winner.count(bi)
    n, bf = len(results), best.get("mean_fold")
    left = results[bi - 1].get("mean_fold") if bi - 1 >= 0 else None
    right = results[bi + 1].get("mean_fold") if bi + 1 < n else None
    present = [c for c in (left, right) if c is not None]
    plateau = bool(present) and bf is not None and bf > 0 and all(c >= 0.8 * bf for c in present)
    mf = [r.get("mean_fold") for r in results]
    at_edge = (bi == 0 or bi == n - 1) and all(x is not None for x in mf) and n >= 3
    monotonic = False
    if at_edge:
        monotonic = (all(mf[i] <= mf[i + 1] + 1e-9 for i in range(n - 1)) if bi == n - 1
                     else all(mf[i] >= mf[i + 1] - 1e-9 for i in range(n - 1)))
    extend = at_edge and monotonic and ordinal
    if n == 2 or not ordinal:   # binaire / catégoriel : pas de voisinage → l'accord entre folds suffit
        plateau = True
    robust = ((best.get("worst_fold") or -1) > 0 and wins >= (NFOLDS + 1) // 2 and (plateau or extend))
    cur = next((r for r in results if r.get("is_current")), None)
    gain = (round(best["mean_fold"] - cur["mean_fold"], 2)
            if cur and best.get("mean_fold") is not None and cur.get("mean_fold") is not None else None)
    sensitivity = round(max(cals) - min(cals), 2)
    # Garde-fous : un paramètre quasi sans effet sur la période complète (Δ < MIN_SENSITIVITY)
    # ou dont l'optimum dégrade le Calmar complet ne se recommande pas — le gain par fold
    # viendrait d'une simple redistribution du PnL entre régimes.
    no_full_loss = cur is not None and best["calmar"] >= cur["calmar"] - 1e-9
    recommend = bool(kind == "param" and robust and gain is not None and gain >= MIN_GAIN
                     and sensitivity >= MIN_SENSITIVITY and no_full_loss)
    return dict(param=name, family=family, kind=kind, ordinal=ordinal, results=results,
                sensitivity=sensitivity, extend=extend,
                gain_vs_current=gain, current_is_best=(cur is best), recommend_change=recommend,
                opt_label=best["label"],
                best_label=(best["label"] if recommend else (cur["label"] if cur else best["label"])),
                best_calmar=best["calmar"], best_worst_fold=best.get("worst_fold"),
                best_mean_fold=best.get("mean_fold"), fold_wins=wins, n_folds=NFOLDS,
                plateau=plateau, robust=robust)


def run(years=4.0):
    surf = fm.fit_surface(min_snapshots=1)
    if surf:
        bt.SKEW_SURFACE = surf["buckets"]; bt.SKEW_POOLED = surf["pooled"]
        n_reg = sum(1 for bk in surf["buckets"] if bk.get("regime_aware"))
        print(f"  Skew fité : {surf['n_snapshots']}j, {len(surf['buckets'])} buckets ({n_reg} régime-aware)")
    else:
        print("  Pas de surface réelle — sweeps sous skew linéaire 0.013")

    # 1) prod d'abord (sans plancher) → fixe le plancher de drawdown des folds
    probe = _run(years, PROD, dd_floor=1e-9)
    dd_floor = max(DD_FLOOR_FRAC * probe["maxdd"], 250.0)
    base = _run(years, PROD, dd_floor, want_curve=True)
    print(f"\n  Config ACTUELLE : PnL {base['pnl']:,}$  MaxDD {base['maxdd']:,}$  Calmar {base['calmar']}  "
          f"frais {base['fees']:,}$  hedge {base['hedge']:+,}$  prime gardée {base['kept_pct']}%")
    print(f"  Plancher de DD des folds : {dd_floor:,.0f}$ ({DD_FLOOR_FRAC:.0%} du MaxDD prod)\n")

    sweeps = []
    for family, name, attrs, values, fmt, kind in SWEEPS:
        results = []
        for v in values:
            st = _run(years, {**PROD, **_as_cfg(attrs, v)}, dd_floor)
            st["label"] = fmt(v)
            st["is_current"] = _is_current(attrs, v)
            results.append(st)
        sweeps.append(_judge(name, family, kind, results, _is_ordinal(attrs, values)))
        s = sweeps[-1]
        tag = f"→ {s['opt_label']} (+{s['gain_vs_current']})" if s["recommend_change"] else "="
        print(f"  [{family:<15}] {name:<42} Δ {s['sensitivity']:>6}  {tag}")

    recos = [f"{s['param']} → {s['opt_label']} (+{s['gain_vs_current']}, {s['fold_wins']}/{NFOLDS})"
             for s in sweeps if s["recommend_change"]]
    print(f"\n  → Changements robustes (gain ≥ {MIN_GAIN} de Calmar moyen, multi-régimes) :")
    print("     " + ("\n     ".join(recos) if recos else "aucun"))

    # Validation conjointe : les recommandations sont mesurées une à une ; plusieurs peuvent
    # agir sur le même levier (ex. trois façons d'alléger le hedge) et ne pas s'additionner.
    combined = None
    chosen = [(spec, s) for spec, s in zip(SWEEPS, sweeps) if s["recommend_change"]]
    if len(chosen) >= 2:
        cfg = dict(PROD)
        for spec, s in chosen:
            v = spec[3][next(i for i, r in enumerate(s["results"]) if r["label"] == s["opt_label"])]
            cfg.update(_as_cfg(spec[2], v))
        combined = _run(years, cfg, dd_floor)
        combined["changes"] = [f"{s['param']} → {s['opt_label']}" for _, s in chosen]
        combined["gain_vs_current"] = (round(combined["mean_fold"] - base["mean_fold"], 2)
                                       if combined.get("mean_fold") is not None else None)
        print(f"  → Toutes ensemble : PnL {combined['pnl']:,}$  MaxDD {combined['maxdd']:,}$  "
              f"Calmar {combined['calmar']}  (moyenne des folds {combined['gain_vs_current']:+} vs actuel)")

    period = {"start": base["curve"][0][0], "end": base["curve"][-1][0], "days": len(base["curve"])}
    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "years": years, "period": period, "n_folds": NFOLDS, "dd_floor": round(dd_floor),
        "min_gain": MIN_GAIN, "min_sensitivity": MIN_SENSITIVITY, "skew_fit": surf, "combined": combined,
        "prod_config": {k: (list(v) if isinstance(v, (list, tuple)) else v) for k, v in PROD.items()},
        "baseline": base, "sweeps": sweeps,
    }
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  → {OUT_FILE} écrit ({len(sweeps)} sweeps, {sum(len(s['results']) for s in sweeps) + 2} backtests).")
    return out


def rejudge(path=OUT_FILE):
    """Recalcule les verdicts d'un backtest_routine.json existant (sans rejouer les backtests),
    utile quand seule la règle de décision change."""
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    cur = {k: (list(v) if isinstance(v, (list, tuple)) else v) for k, v in PROD.items()}
    diff = {k: (d.get("prod_config", {}).get(k), v) for k, v in cur.items() if d.get("prod_config", {}).get(k) != v}
    if diff:
        # Les « is_current » du JSON désignent l'ancienne config : les verdicts seraient faux.
        raise SystemExit(f"Config de prod changée depuis la routine ({diff}) — relancer la routine complète.")
    d["sweeps"] = [_judge(s["param"], s.get("family", "?"), s.get("kind", "param"), s["results"],
                          s.get("ordinal", True)) for s in d["sweeps"]]
    d["min_gain"], d["min_sensitivity"] = MIN_GAIN, MIN_SENSITIVITY
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False, default=str)
    return [(s["param"], s["opt_label"], s["gain_vs_current"]) for s in d["sweeps"] if s["recommend_change"]]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument('--years', type=float, default=4.0)
    ap.add_argument('--rejudge', action='store_true', help='recalcule seulement les verdicts du JSON existant')
    a = ap.parse_args()
    if a.rejudge:
        print(rejudge())
    else:
        run(a.years)
