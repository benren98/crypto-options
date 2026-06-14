"""
fit_vol_model.py — Fitte notre propre modèle de skew sur les VRAIES surfaces
collectées (vol_surface.jsonl), puis l'écrit dans vol_model_fit.json pour que le
backtest le projette dans le passé (au lieu du SKEW_SLOPE=0.013 linéaire deviné).

Modèle fité (puts OTM) : IV(K) / IV_ATM = 1 + a·OTM% + b·OTM%²
    — le terme quadratique capture la CONVEXITÉ du skew réel que le modèle
      linéaire d'origine rate (le deep-OTM est bien plus cher que 1+0.013·OTM%).

N'écrit le fichier que si assez de jours collectés (MIN_SNAPSHOTS), sinon le
backtest garde le modèle linéaire par défaut.

Usage : python fit_vol_model.py
"""
import json, sys
from datetime import datetime, timezone
sys.path.insert(0, '.')
import numpy as np
import vol_surface_data as vs

OUT_FILE      = "vol_model_fit.json"
MIN_SNAPSHOTS = 15      # jours minimum avant de fitter
MIN_POINTS    = 200


def fit():
    cov = vs.coverage()
    if not cov or cov["days"] < MIN_SNAPSHOTS:
        n = cov["days"] if cov else 0
        print(f"Données insuffisantes : {n} jours collectés (< {MIN_SNAPSHOTS}). "
              f"Le backtest garde le skew linéaire par défaut. Laisse accumuler.")
        return None

    # Points (OTM%, ratio = IV/IV_ATM) sur les puts OTM
    otm, ratio = [], []
    for d, dte, mny, iv, atm in vs.all_points():
        if mny is None or iv is None or not atm or mny >= 1.0:
            continue
        otm.append((1.0 - mny) * 100.0)
        ratio.append(iv / atm)
    if len(otm) < MIN_POINTS:
        print(f"Trop peu de points OTM ({len(otm)} < {MIN_POINTS}).")
        return None

    otm = np.array(otm); ratio = np.array(ratio)
    # (ratio - 1) = a·OTM + b·OTM²   (intercept forcé à 0 → ratio=1 à l'ATM)
    X = np.column_stack([otm, otm**2])
    coef, *_ = np.linalg.lstsq(X, ratio - 1.0, rcond=None)
    a, b = float(coef[0]), float(coef[1])
    pred = 1.0 + a*otm + b*otm**2
    ss_res = float(np.sum((ratio - pred)**2))
    ss_tot = float(np.sum((ratio - ratio.mean())**2))
    r2 = 1.0 - ss_res/ss_tot if ss_tot > 0 else 0.0

    fit = {
        "a": round(a, 6), "b": round(b, 8),
        "form": "iv_ratio = 1 + a*OTM% + b*OTM%^2",
        "n_points": len(otm), "n_snapshots": cov["days"],
        "coverage": cov, "r2": round(r2, 4),
        "fitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "linear_equiv_slope": round(a, 6),   # pour comparer au SKEW_SLOPE=0.013 d'origine
    }
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(fit, f, indent=2)
    print(f"Fit écrit dans {OUT_FILE} :")
    print(f"  ratio = 1 + {a:.4f}·OTM% + {b:.6f}·OTM%²   (R²={r2:.3f}, {len(otm)} pts sur {cov['days']}j)")
    print(f"  vs modèle d'origine : 1 + 0.013·OTM% (linéaire)")
    # exemples
    for o in (5, 10, 15, 20):
        print(f"   OTM {o:>2}% → fité ×{1+a*o+b*o*o:.3f}  | linéaire ×{1+0.013*o:.3f}")
    return fit


if __name__ == "__main__":
    fit()
