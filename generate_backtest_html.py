"""
generate_backtest_html.py — docs/backtest.html : backtests + surface de vol, orientée décision.

Même architecture que le dashboard v2 : ce script calcule un modèle de données, le template
backtest_page.html le rend (CSS/JS communs de dashboard_assets/).

Sections :
  • Backtest de la config de prod : KPIs, equity, attribution (options / hedge / funding / frais).
  • Décisions : changements robustes proposés par la routine (jamais appliqués automatiquement).
  • Sweeps par famille (scoring, entrée, sizing, hedge, circuit breaker) + hypothèses stressées.
  • Surface de vol — notre modèle vs le marché :
      - smile du dernier instantané, par échéance : marks du marché, « notre mark » (celui que
        le backtest attribue : DVOL × skew fité) et le smile du jour (lissage quadratique) ;
      - écarts option par option, avec signal NET DU SPREAD (achat si la référence dépasse la
        vol à l'ask estimée, vente si la vol au bid dépasse la référence) et valeur en $ via vega ;
      - qualité du fit dans le temps (biais et RMSE en points de vol) et biais par zone
        moneyness × maturité (où le modèle se trompe systématiquement).

Sources : backtest_routine.json, vol_surface.jsonl, vol_model_fit.json (repli : fit de la routine).
Usage : python generate_backtest_html.py
"""
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT     = Path(__file__).parent
TEMPLATE = ROOT / "backtest_page.html"
OUT      = ROOT / "docs" / "backtest.html"
OTM_BANDS = [(0, 3), (3, 6), (6, 10), (10, 15), (15, 20), (20, 30), (30, 60)]
DTE_BANDS = [(0, 9, "≤9 j"), (9, 16, "9-16 j"), (16, 45, ">16 j")]


def _load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _load_surfaces(path=ROOT / "vol_surface.jsonl"):
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


# ── Modèle de skew (même formule que backtest.skew_factor) ─────────────────────
def _bucket(fit, dte):
    for b in (fit or {}).get("buckets") or []:
        if b["dte_lo"] <= dte < b["dte_hi"]:
            return b
    return (fit or {}).get("pooled")


def model_level(fit, dte, dvol):
    """ATM de l'échéance / DVOL (même formule que backtest.level_factor)."""
    bk = _bucket(fit, dte)
    if not bk or "l0" not in bk:
        return 1.0
    dc = (dvol - bk.get("l_ref", 0.0)) if dvol is not None else 0.0
    return max(0.5, bk["l0"] + bk.get("l1", 0.0) * dc)


def model_ratio(fit, otm, dte, dvol):
    o = max(otm, 0.0)
    bk = _bucket(fit, dte)
    if not bk:
        return 1.0 + 0.013 * o
    dc = (dvol - bk.get("dvol_ref", 0.0)) if dvol is not None else 0.0
    a = bk["a0"] + bk.get("a1", 0.0) * dc
    b = bk["b0"] + bk.get("b1", 0.0) * dc
    return 1.0 + a * o + b * o * o


def vega_usd(S, K, T, iv_pct):
    """Vega BS (USD par point de vol, pour 1 BTC de notionnel)."""
    if T <= 0 or iv_pct <= 0:
        return 0.0
    s = iv_pct / 100
    d1 = (math.log(S / K) + 0.5 * s * s * T) / (s * math.sqrt(T))
    return S * math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) * math.sqrt(T) / 100


def _smile_fit(otms, ratios):
    """Lissage du jour : ratio − 1 = a·otm + b·otm² (même forme que le modèle)."""
    if len(otms) < 3:
        return None
    X = np.column_stack([otms, np.square(otms)])
    coef, *_ = np.linalg.lstsq(X, np.asarray(ratios) - 1.0, rcond=None)
    return float(coef[0]), float(coef[1])


def _points(snap, fit):
    """Points OTM d'un instantané avec notre mark, le smile du jour et les écarts."""
    S, dvol = float(snap.get("spot") or 0), snap.get("dvol")
    rows = []
    for e in snap.get("expiries", []):
        dte, atm = float(e.get("dte") or 0), e.get("atm_iv")
        if not atm or dte <= 0:
            continue
        pts = [s for s in e.get("strikes", []) if s.get("mark_iv") and s.get("moneyness") is not None
               and s["moneyness"] <= 1.0]
        otms = [(1 - s["moneyness"]) * 100 for s in pts]
        smile = _smile_fit(otms, [s["mark_iv"] / atm for s in pts])
        for s, otm in zip(pts, otms):
            mark = float(s["mark_iv"])
            bid = float(s["bid_iv"]) if s.get("bid_iv") else None
            ask = (2 * mark - bid) if bid else None          # demi-spread symétrique en vol
            ours = (float(dvol) * model_level(fit, dte, dvol) * model_ratio(fit, otm, dte, dvol)) if dvol else None
            day = atm * (1 + smile[0] * otm + smile[1] * otm * otm) if smile else None
            vg = vega_usd(S, float(s["strike"]), dte / 365, mark)

            def signal(ref):
                if ref is None or bid is None:
                    return None, 0.0
                if ref > ask:
                    return "achat", ref - ask
                if bid > ref:
                    return "vente", bid - ref
                return None, 0.0

            sig_m, edge_m = signal(ours)
            sig_d, edge_d = signal(day)
            rows.append({
                "expiry": e.get("expiry"), "dte": round(dte, 1), "strike": s["strike"], "otm": round(otm, 2),
                "mark_iv": round(mark, 2), "bid_iv": round(bid, 2) if bid else None,
                "ask_iv": round(ask, 2) if ask else None,
                "ours": round(ours, 2) if ours else None, "day": round(day, 2) if day else None,
                "gap_ours": round(ours - mark, 2) if ours else None,
                "gap_day": round(day - mark, 2) if day else None,
                "sig_ours": sig_m, "edge_ours": round(edge_m, 2), "usd_ours": round(edge_m * vg, 1),
                "sig_day": sig_d, "edge_day": round(edge_d, 2), "usd_day": round(edge_d * vg, 1),
                "vega": round(vg, 2), "oi": s.get("oi"), "vol24h": s.get("vol24h"),
            })
    return rows


def vol_analysis(surfaces, fit):
    if not surfaces:
        return None
    last = surfaces[-1]
    pts = _points(last, fit)
    smiles = []
    for e in last.get("expiries", []):
        sub = [p for p in pts if p["expiry"] == e.get("expiry")]
        if sub:
            smiles.append({"expiry": e.get("expiry"), "dte": round(float(e.get("dte") or 0), 1),
                           "atm_iv": e.get("atm_iv"), "points": sorted(sub, key=lambda p: p["otm"])})
    history, cells = [], defaultdict(list)
    for snap in surfaces:
        p = _points(snap, fit)
        g = [x["gap_ours"] for x in p if x["gap_ours"] is not None]
        lvl = [float(snap["dvol"]) - float(e["atm_iv"]) for e in snap.get("expiries", [])
               if snap.get("dvol") and e.get("atm_iv")]
        if g:
            history.append({"d": snap.get("date"), "bias": round(float(np.mean(g)), 2),
                            "rmse": round(float(np.sqrt(np.mean(np.square(g)))), 2),
                            "level": round(float(np.mean(lvl)), 2) if lvl else None,
                            "dvol": snap.get("dvol")})
        for x in p:
            if x["gap_ours"] is None:
                continue
            ob = next((i for i, (lo, hi) in enumerate(OTM_BANDS) if lo <= x["otm"] < hi), None)
            db = next((i for i, (lo, hi, _) in enumerate(DTE_BANDS) if lo <= x["dte"] < hi), None)
            if ob is not None and db is not None:
                cells[(ob, db)].append(x["gap_ours"])
    grid = [[{"mean": round(float(np.mean(cells[(i, j)])), 2), "n": len(cells[(i, j)])} if cells.get((i, j)) else None
             for j in range(len(DTE_BANDS))] for i in range(len(OTM_BANDS))]
    opps = sorted([p for p in pts if p["sig_day"] or p["sig_ours"]],
                  key=lambda p: -max(p["usd_day"], p["usd_ours"]))
    return {
        "date": last.get("date"), "ts": last.get("ts"), "spot": last.get("spot"), "dvol": last.get("dvol"),
        "n_snapshots": len(surfaces), "smiles": smiles, "points": sorted(pts, key=lambda p: (p["expiry"], p["otm"])),
        "opportunities": opps[:12], "history": history,
        "bias_grid": {"otm": [f"{lo}-{hi} %" for lo, hi in OTM_BANDS], "dte": [lab for *_, lab in DTE_BANDS], "cells": grid},
        "fit": {"buckets": [{k: b.get(k) for k in ("label", "a0", "b0", "a1", "b1", "r2", "n", "dvol_ref",
                                                    "regime_aware", "dvol_spread", "l0", "l1")} for b in (fit or {}).get("buckets") or []],
                "coverage": (fit or {}).get("coverage"), "fitted_at": (fit or {}).get("fitted_at")},
    }


def build():
    routine = _load_json(ROOT / "backtest_routine.json", {}) or {}
    fit = _load_json(ROOT / "vol_model_fit.json") or routine.get("skew_fit")
    surfaces = _load_surfaces()
    base = routine.get("baseline") or {}
    sweeps = routine.get("sweeps") or []
    families = []
    for s in sweeps:
        fam = s.get("family", "Autres")
        if not families or families[-1]["name"] != fam:
            families.append({"name": fam, "sweeps": []})
        families[-1]["sweeps"].append(s)
    recos = [{"param": s["param"], "family": s.get("family"), "current": next((r["label"] for r in s["results"] if r.get("is_current")), "?"),
              "proposed": s.get("opt_label"), "gain": s.get("gain_vs_current"), "wins": s.get("fold_wins"),
              "n": s.get("n_folds")} for s in sweeps if s.get("recommend_change")]
    return {
        "meta": {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "routine_at": routine.get("generated_at"), "period": routine.get("period"),
                 "n_folds": routine.get("n_folds", 5), "dd_floor": routine.get("dd_floor"),
                 "min_gain": routine.get("min_gain", 1.0), "min_sensitivity": routine.get("min_sensitivity", 0.5),
                 "fold_dates": (base.get("fold_dates") or [])},
        "baseline": base, "families": families, "recommendations": recos,
        "combined": ({k: v for k, v in routine["combined"].items() if k != "curve"}
                     if routine.get("combined") else None),
        "vol": vol_analysis(surfaces, fit),
    }


def generate():
    import generate_dashboard as gd     # rendu partagé (assets communs + JSON embarqué)
    model = build()
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(gd.render(model, TEMPLATE), encoding="utf-8")
    v = model.get("vol") or {}
    print(f"backtest.html généré ({OUT.stat().st_size / 1024:.0f} KB) — "
          f"{len(model['families'])} familles de sweeps, {len(model['recommendations'])} reco, "
          f"{len(v.get('opportunities') or [])} écarts de vol au-delà du spread")


if __name__ == "__main__":
    generate()
