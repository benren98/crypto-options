"""
fit_vol_model.py — Fitte notre modèle de skew sur les VRAIES surfaces collectées
(vol_surface.jsonl), PAR MATURITÉ et CONDITIONNÉ AU RÉGIME DE VOL (DVOL).

Pour chaque bucket de maturité :
    IV(K)/IV_ATM = 1 + a(DVOL)·OTM% + b(DVOL)·OTM%²
    avec  a(DVOL) = a0 + a1·(DVOL − DVOL_ref),  b(DVOL) = b0 + b1·(DVOL − DVOL_ref)

  • le quadratique capture la convexité du skew ;
  • le découpage par maturité capture la pentification à court terme (term structure) ;
  • la dépendance en DVOL (a1, b1) déforme le skew selon le régime → quand on projette
    sur un crash passé (DVOL haute), on applique un skew plus pentu, comme observé.

Les pentes de régime (a1, b1) ne sont fittées que si les données couvrent assez
d'amplitude de DVOL (MIN_DVOL_SPREAD) ; sinon a1=b1=0 (skew statique). Repli poolé
puis linéaire. Le fichier de prod n'est écrit qu'à ≥ MIN_SNAPSHOTS jours ;
fit_surface(min_snapshots=1) reste utilisable en mémoire (routine, aperçu).

Usage : python fit_vol_model.py
"""
import json, sys
from datetime import datetime, timezone
sys.path.insert(0, '.')
import numpy as np
import vol_surface_data as vs

OUT_FILE        = "vol_model_fit.json"
MIN_SNAPSHOTS   = 15      # jours min avant d'écrire le fichier de prod
MIN_POINTS      = 40      # points min par bucket pour le fitter (sinon repli poolé)
MIN_DVOL_SPREAD = 15.0    # amplitude DVOL (pts) min pour activer la dépendance régime

BUCKETS = [(0, 9, "≤9j"), (9, 16, "9-16j"), (16, 45, ">16j")]


def _fit(otm, ratio, dvol):
    """Fit (ratio−1) en fonction de OTM, OTM², et leurs interactions avec ΔDVOL si
    l'amplitude de DVOL le permet. Retourne un dict de coefficients régime-aware."""
    otm = np.asarray(otm, float); ratio = np.asarray(ratio, float); dvol = np.asarray(dvol, float)
    n = len(otm)
    if n < 3:
        return None
    dvol_ref = float(np.nanmean(dvol))
    dc = dvol - dvol_ref
    spread = float(np.nanmax(dvol) - np.nanmin(dvol)) if np.isfinite(dvol).any() else 0.0
    regime = spread >= MIN_DVOL_SPREAD and np.isfinite(dc).all()

    if regime:
        X = np.column_stack([otm, otm*dc, otm**2, otm**2*dc])
        coef, *_ = np.linalg.lstsq(X, ratio - 1.0, rcond=None)
        a0, a1, b0, b1 = (float(c) for c in coef)
    else:
        X = np.column_stack([otm, otm**2])
        coef, *_ = np.linalg.lstsq(X, ratio - 1.0, rcond=None)
        a0, b0 = float(coef[0]), float(coef[1]); a1 = b1 = 0.0

    pred = 1 + (a0 + a1*dc)*otm + (b0 + b1*dc)*otm**2
    ss_tot = float(np.sum((ratio - ratio.mean())**2))
    r2 = 1 - float(np.sum((ratio - pred)**2))/ss_tot if ss_tot > 0 else 0.0
    return {"a0": round(a0, 6), "a1": round(a1, 7), "b0": round(b0, 8), "b1": round(b1, 9),
            "dvol_ref": round(dvol_ref, 2), "regime_aware": bool(regime),
            "dvol_spread": round(spread, 1), "r2": round(r2, 4), "n": n}


def fit_surface(min_snapshots: int = MIN_SNAPSHOTS):
    cov = vs.coverage()
    if not cov or cov["days"] < min_snapshots:
        return None

    pooled_pts = ([], [], [])
    by_bucket = {lab: ([], [], []) for _, _, lab in BUCKETS}
    for d, dte, mny, iv, atm, dv in vs.all_points_dvol():
        if mny is None or iv is None or not atm or mny >= 1.0 or dte is None or dv is None:
            continue
        otm = (1.0 - mny) * 100.0; r = iv / atm
        pooled_pts[0].append(otm); pooled_pts[1].append(r); pooled_pts[2].append(dv)
        for lo, hi, lab in BUCKETS:
            if lo <= dte < hi:
                by_bucket[lab][0].append(otm); by_bucket[lab][1].append(r); by_bucket[lab][2].append(dv)
                break

    pooled = _fit(*pooled_pts)
    if pooled is None:
        return None

    buckets = []
    for lo, hi, lab in BUCKETS:
        o, r, dv = by_bucket[lab]
        fit = _fit(o, r, dv) if len(o) >= MIN_POINTS else None
        if fit is None:
            fit = dict(pooled); fit["n"] = len(o); fit["from_pooled"] = True
        fit.update({"dte_lo": lo, "dte_hi": hi, "label": lab})
        buckets.append(fit)

    return {
        "form": "iv_ratio = 1 + a(DVOL)*OTM% + b(DVOL)*OTM%^2, par bucket de maturité",
        "buckets": buckets, "pooled": pooled,
        "coverage": cov, "n_snapshots": cov["days"],
        "fitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


def main():
    surf = fit_surface(MIN_SNAPSHOTS)
    if surf is None:
        cov = vs.coverage(); n = cov["days"] if cov else 0
        print(f"Données insuffisantes : {n} jours (< {MIN_SNAPSHOTS}). Skew linéaire par défaut. "
              f"(Routine : fit_surface(1) reste utilisable en mémoire.)")
        return None
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(surf, f, indent=2, ensure_ascii=False)
    print(f"Surface écrite dans {OUT_FILE} ({surf['n_snapshots']}j) :")
    for bk in surf["buckets"]:
        reg = "régime-aware" if bk["regime_aware"] else "statique"
        print(f"  {bk['label']:>6} : a={bk['a0']:.4f}{bk['a1']:+.5f}·ΔDVOL  b={bk['b0']:.5f}{bk['b1']:+.7f}·ΔDVOL "
              f"(ref {bk['dvol_ref']}, spread {bk['dvol_spread']}, {reg}, R²={bk['r2']}, n={bk['n']})")
    return surf


if __name__ == "__main__":
    main()
