"""
vol_surface_data.py — Accès au dataset de surfaces de vol réelles (vol_surface.jsonl,
collecté par vol_surface_logger.py).

Fournit au backtest :
  • coverage()              → {start, end, days} du réel enregistré, ou None
  • iv_for(date,dte,mny)    → mark IV réelle interpolée (%), ou None si non couvert
  • skew_ratio(date,dte,mny)→ IV/ATM réel (forme du skew, niveau découplé)

Interpolation : échéance la plus proche en DTE, puis interpolation linéaire de
l'IV en moneyness (strike/spot) entre les strikes enregistrés (clampée aux bords).
"""
import json, os
from datetime import date as _date

LOG_FILE = "vol_surface.jsonl"
_CACHE = None


def _load():
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    snaps = {}
    if os.path.exists(LOG_FILE):
        for line in open(LOG_FILE, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                s = json.loads(line)
                snaps[s["date"]] = s
            except Exception:
                continue
    _CACHE = snaps
    return _CACHE


def reload():
    global _CACHE
    _CACHE = None
    return _load()


def coverage():
    snaps = _load()
    if not snaps:
        return None
    ds = sorted(snaps)
    return {"start": ds[0], "end": ds[-1], "days": len(ds)}


def _to_iso(d):
    if isinstance(d, str):
        return d[:10]
    if isinstance(d, _date):
        return d.isoformat()
    return str(d)[:10]


def _interp_mny(strikes, mny, field="mark_iv"):
    """Interpolation linéaire de `field` en moneyness sur des strikes triés."""
    pts = [(s["moneyness"], s.get(field)) for s in strikes if s.get(field) is not None]
    if not pts:
        return None
    pts.sort()
    if mny <= pts[0][0]:
        return pts[0][1]
    if mny >= pts[-1][0]:
        return pts[-1][1]
    for i in range(1, len(pts)):
        if mny <= pts[i][0]:
            x0, y0 = pts[i-1]; x1, y1 = pts[i]
            w = (mny - x0) / (x1 - x0) if x1 != x0 else 0
            return y0 + w * (y1 - y0)
    return pts[-1][1]


def _expiry_for(snap, dte):
    exps = snap.get("expiries", [])
    if not exps:
        return None
    return min(exps, key=lambda e: abs(e.get("dte", 1e9) - dte))


def iv_for(d, dte, moneyness):
    """mark IV réelle (%) pour (date, DTE cible, moneyness=strike/spot), ou None."""
    snap = _load().get(_to_iso(d))
    if snap is None:
        return None
    exp = _expiry_for(snap, dte)
    if exp is None:
        return None
    return _interp_mny(exp["strikes"], moneyness, "mark_iv")


def atm_for(d, dte):
    snap = _load().get(_to_iso(d))
    if snap is None:
        return None
    exp = _expiry_for(snap, dte)
    return exp.get("atm_iv") if exp else None


def skew_ratio(d, dte, moneyness):
    """IV(strike) / IV(ATM) réel — forme du skew, indépendante du niveau."""
    iv = iv_for(d, dte, moneyness)
    atm = atm_for(d, dte)
    if iv is None or not atm:
        return None
    return iv / atm


def all_points():
    """Itère (date, dte, moneyness, mark_iv, atm_iv) sur tout le dataset (pour le fit)."""
    for d, snap in _load().items():
        for exp in snap.get("expiries", []):
            atm = exp.get("atm_iv")
            for s in exp.get("strikes", []):
                if s.get("mark_iv") is not None:
                    yield (d, exp.get("dte"), s.get("moneyness"), s.get("mark_iv"), atm)


def all_points_dvol():
    """Comme all_points mais ajoute la DVOL du snapshot : (date, dte, moneyness,
    mark_iv, atm_iv, dvol). Pour le fit du skew conditionné au régime de vol."""
    for d, snap in _load().items():
        dv = snap.get("dvol")
        for exp in snap.get("expiries", []):
            atm = exp.get("atm_iv")
            for s in exp.get("strikes", []):
                if s.get("mark_iv") is not None:
                    yield (d, exp.get("dte"), s.get("moneyness"), s.get("mark_iv"), atm, dv)


if __name__ == "__main__":
    cov = coverage()
    print("Couverture :", cov)
    if cov:
        d = cov["end"]
        print(f"Exemple {d} (dte 7) :")
        for mny in (0.80, 0.85, 0.90, 0.95, 1.0):
            r = skew_ratio(d, 7, mny)
            print(f"  mny {mny:.2f} → IV {iv_for(d, 7, mny)}  (skew ×{round(r,3) if r else None})")
