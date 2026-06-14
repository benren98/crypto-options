"""
fit_vol_model.py — Fitte notre modèle de skew sur les VRAIES surfaces collectées
(vol_surface.jsonl), PAR MATURITÉ (vraie surface, pas une courbe unique).

Pour chaque bucket de maturité, fit : IV(K)/IV_ATM = 1 + a·OTM% + b·OTM%²
    — le terme quadratique capture la convexité ; le découpage par maturité capture
      la pentification du skew quand l'échéance raccourcit (term structure).

Écrit vol_model_fit.json (buckets + fit poolé en repli). Le fichier committé n'est
écrit que si ≥ MIN_SNAPSHOTS jours ; mais fit_surface(min_snapshots=1) est exposé
pour un usage en mémoire (routine de backtests, aperçu) dès le premier jour.

Usage : python fit_vol_model.py
"""
import json, sys
from datetime import datetime, timezone
sys.path.insert(0, '.')
import numpy as np
import vol_surface_data as vs

OUT_FILE      = "vol_model_fit.json"
MIN_SNAPSHOTS = 15      # jours min avant d'écrire le fichier de prod
MIN_POINTS    = 40      # points min par bucket pour le fitter (sinon repli poolé)

# Buckets de maturité (jours). Adaptés à la fenêtre du collecteur (≈5-30j).
BUCKETS = [(0, 9, "≤9j"), (9, 16, "9-16j"), (16, 45, ">16j")]


def _fit_ab(otm, ratio):
    """Fit (ratio−1) = a·OTM + b·OTM² par moindres carrés. Retourne (a, b, r2, n)."""
    otm = np.asarray(otm); ratio = np.asarray(ratio)
    if len(otm) < 3:
        return None
    X = np.column_stack([otm, otm**2])
    coef, *_ = np.linalg.lstsq(X, ratio - 1.0, rcond=None)
    a, b = float(coef[0]), float(coef[1])
    pred = 1 + a*otm + b*otm**2
    ss_tot = float(np.sum((ratio - ratio.mean())**2))
    r2 = 1 - float(np.sum((ratio - pred)**2))/ss_tot if ss_tot > 0 else 0.0
    return a, b, round(r2, 4), len(otm)


def fit_surface(min_snapshots: int = MIN_SNAPSHOTS):
    """Retourne la surface fittée (dict) ou None si données insuffisantes."""
    cov = vs.coverage()
    if not cov or cov["days"] < min_snapshots:
        return None

    # Collecte des points OTM puts, groupés par bucket de maturité
    pooled_otm, pooled_ratio = [], []
    by_bucket = {lab: ([], []) for _, _, lab in BUCKETS}
    for d, dte, mny, iv, atm in vs.all_points():
        if mny is None or iv is None or not atm or mny >= 1.0 or dte is None:
            continue
        otm = (1.0 - mny) * 100.0
        r = iv / atm
        pooled_otm.append(otm); pooled_ratio.append(r)
        for lo, hi, lab in BUCKETS:
            if lo <= dte < hi:
                by_bucket[lab][0].append(otm); by_bucket[lab][1].append(r)
                break

    pooled = _fit_ab(pooled_otm, pooled_ratio)
    if pooled is None:
        return None
    p_a, p_b, p_r2, p_n = pooled

    buckets = []
    for lo, hi, lab in BUCKETS:
        o, r = by_bucket[lab]
        fit = _fit_ab(o, r) if len(o) >= MIN_POINTS else None
        if fit is None:
            a, b, r2, n = p_a, p_b, p_r2, len(o)   # repli sur le poolé
            fitted = False
        else:
            a, b, r2, n = fit; fitted = True
        buckets.append({"dte_lo": lo, "dte_hi": hi, "label": lab,
                        "a": round(a, 6), "b": round(b, 8), "r2": r2,
                        "n": n, "fitted": fitted})

    return {
        "form": "iv_ratio = 1 + a*OTM% + b*OTM%^2, par bucket de maturité",
        "buckets": buckets,
        "pooled": {"a": round(p_a, 6), "b": round(p_b, 8), "r2": p_r2, "n": p_n},
        "coverage": cov, "n_snapshots": cov["days"],
        "fitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


def main():
    surf = fit_surface(MIN_SNAPSHOTS)
    if surf is None:
        cov = vs.coverage(); n = cov["days"] if cov else 0
        print(f"Données insuffisantes : {n} jours (< {MIN_SNAPSHOTS}). Le backtest garde le "
              f"skew linéaire par défaut. (Routine : fit_surface(1) reste utilisable en mémoire.)")
        return None
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(surf, f, indent=2, ensure_ascii=False)
    print(f"Surface écrite dans {OUT_FILE} ({surf['n_snapshots']}j) :")
    for bk in surf["buckets"]:
        tag = "fit" if bk["fitted"] else "poolé"
        print(f"  {bk['label']:>6} : 1 + {bk['a']:.4f}·OTM + {bk['b']:.6f}·OTM²  "
              f"(R²={bk['r2']}, n={bk['n']}, {tag})")
    return surf


if __name__ == "__main__":
    main()
