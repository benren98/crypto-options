"""
autocall_market.py — Données de marché du pricer d'autocall BTC : surface SSVI calibrée sur Deribit.

Pour chaque échéance listée (TTE ≥ MIN_TTE_DAYS) :
  · forward F = underlying_price de Deribit (future synthétique de l'échéance) ;
  · options hors de la monnaie (puts si K < F, calls sinon) : IV mark, IV bid/ask recalculées en
    Black-76 sur F (les prix Deribit sont en BTC : prix USD = prix BTC × F).
Surface SSVI (Gatheral-Jacquier), sans arbitrage calendaire si θ(t) croît :
    w(k, t) = θ_t / 2 · (1 + ρ φ k + √((φ k + ρ)² + 1 − ρ²)),   k = ln(K / F_t),  w = σ² t
    φ(θ) = η / (θ^γ (1 + θ)^(1−γ))      (sans arbitrage papillon si η (1 + |ρ|) ≤ 2)
avec ρ et η qui varient avec la maturité (ρ(θ) = ρ0 + ρ1·√θ, idem η) : le skew du BTC est bien plus
pentu sur les échéances courtes. θ_t est l'ATM de chaque échéance (variance totale), rendu croissant.

Sortie : autocall_market.json (lu par generate_autocall_html.py → docs/autocall.html).

Usage : python autocall_market.py [--out autocall_market.json]
"""
import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.optimize import least_squares
from scipy.stats import norm

HERE = Path(__file__).parent
MIN_TTE_DAYS = 2.0        # les échéances plus courtes sont trop bruitées pour un produit de plusieurs mois
MIN_ABS_DELTA = 0.03      # ailes extrêmes exclues de la calibration (cotations peu fiables)
USD_RATE = 0.04           # taux sans risque USD par défaut (actualisation des flux de l'autocall)


def b76(F, K, T, sig, call):
    """Prix Black-76 non actualisé (USD) d'une option sur F."""
    if T <= 0 or sig <= 0:
        return max(F - K, 0.0) if call else max(K - F, 0.0)
    sq = sig * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sq * sq) / sq
    d2 = d1 - sq
    return F * norm.cdf(d1) - K * norm.cdf(d2) if call else K * norm.cdf(-d2) - F * norm.cdf(-d1)


def implied_vol(price, F, K, T, call):
    """Volatilité implicite Black-76 par dichotomie (None si le prix sort des bornes)."""
    if price is None or price <= 0 or T <= 0:
        return None
    intrinsic = max(F - K, 0.0) if call else max(K - F, 0.0)
    if price <= intrinsic + 1e-9 or price >= (F if call else K):
        return None
    lo, hi = 1e-3, 6.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if b76(F, K, T, mid, call) > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def ssvi_w(k, theta, rho, eta, gamma):
    phi = eta / (theta ** gamma * (1 + theta) ** (1 - gamma))
    return theta / 2 * (1 + rho * phi * k + np.sqrt((phi * k + rho) ** 2 + 1 - rho ** 2))


def rho_eta(theta, p):
    """ρ et η dépendant de la maturité via √θ (bornés pour rester sans arbitrage papillon)."""
    rho = np.clip(p[0] + p[1] * np.sqrt(theta), -0.98, 0.98)
    eta = np.clip(p[2] + p[3] * np.sqrt(theta), 0.02, 2.0 / (1 + np.abs(rho)) - 1e-6)
    return rho, eta


def fetch_quotes():
    import greeks_hedge as g
    spot = g.fetch_spot("BTC")
    chain = g.fetch_option_chain("BTC")
    by_exp = {}
    for r in chain.values():
        t_days = r.get("tte_days") or 0
        if t_days < MIN_TTE_DAYS:
            continue
        by_exp.setdefault(r["expiration_timestamp"], []).append(r)
    expiries = []
    for exp_ts, rows in sorted(by_exp.items()):
        F = rows[0].get("underlying_price")
        T = rows[0]["tte_days"] / 365
        if not F or T <= 0:
            continue
        quotes = []
        for r in rows:
            K, call = float(r["strike"]), r.get("option_type") == "call"
            if call != (K >= F) or not r.get("mark_iv"):
                continue                      # côté hors de la monnaie seulement
            iv = r["mark_iv"] / 100
            sq = iv * math.sqrt(T)
            d1 = (math.log(F / K) + 0.5 * sq * sq) / sq
            delta = norm.cdf(d1) if call else norm.cdf(d1) - 1
            bid, ask = r.get("best_bid_price") or 0, r.get("best_ask_price") or 0
            quotes.append({
                "K": K, "k": math.log(K / F), "call": call, "iv": iv, "delta": delta,
                "iv_bid": implied_vol(bid * F, F, K, T, call) if bid else None,
                "iv_ask": implied_vol(ask * F, F, K, T, call) if ask else None,
                "oi": r.get("open_interest") or 0,
            })
        quotes.sort(key=lambda q: q["k"])
        if len(quotes) >= 5:
            expiries.append({"expiry": datetime.fromtimestamp(exp_ts / 1000, tz=timezone.utc).isoformat(),
                             "t": T, "F": F, "quotes": quotes})
    return spot, expiries


def atm_theta(e):
    """Variance totale ATM : IV interpolée en k = 0 entre les deux cotations qui encadrent le forward."""
    ks = np.array([q["k"] for q in e["quotes"]]); ivs = np.array([q["iv"] for q in e["quotes"]])
    return float(np.interp(0.0, ks, ivs)) ** 2 * e["t"]


def fit(spot, expiries):
    th = np.maximum.accumulate(np.array([atm_theta(e) for e in expiries]))   # calendrier sans arbitrage
    rows = [(i, q["k"], q["iv"]) for i, e in enumerate(expiries) for q in e["quotes"]
            if abs(q["delta"]) >= MIN_ABS_DELTA]
    idx = np.array([r[0] for r in rows]); k = np.array([r[1] for r in rows]); iv = np.array([r[2] for r in rows])
    T = np.array([expiries[i]["t"] for i in idx]); theta = th[idx]

    def resid(p):
        rho, eta = rho_eta(theta, p)
        w = ssvi_w(k, theta, rho, eta, p[4])
        return np.sqrt(np.maximum(w, 1e-12) / T) - iv

    p0 = np.array([-0.3, 0.0, 0.8, 0.0, 0.4])
    res = least_squares(resid, p0, bounds=([-0.98, -3, 0.02, -5, 0.01], [0.98, 3, 2.0, 5, 0.99]))
    p = res.x
    err = resid(p)
    for i, e in enumerate(expiries):
        e["theta"] = float(th[i])
        m = idx == i
        e["rmse_volpts"] = round(float(np.sqrt(np.mean(err[m] ** 2))) * 100, 2) if m.any() else None
        rho_i, eta_i = rho_eta(np.array([th[i]]), p)
        e["rho"], e["eta"] = round(float(rho_i[0]), 4), round(float(eta_i[0]), 4)
    return {"rho0": p[0], "rho1": p[1], "eta0": p[2], "eta1": p[3], "gamma": p[4],
            "rmse_volpts": round(float(np.sqrt(np.mean(err ** 2))) * 100, 2), "n_quotes": int(len(err))}


def build(out_path=HERE / "autocall_market.json"):
    spot, expiries = fetch_quotes()
    params = fit(spot, expiries)
    data = {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "underlying": "BTC", "spot": spot, "usd_rate": USD_RATE,
        "ssvi": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in params.items()},
        "expiries": [{
            "expiry": e["expiry"], "t": round(e["t"], 6), "F": e["F"], "theta": round(e["theta"], 6),
            "rho": e["rho"], "eta": e["eta"], "rmse_volpts": e["rmse_volpts"],
            "quotes": [[round(q["K"]), round(q["k"], 5), round(q["iv"] * 100, 2),
                        round(q["iv_bid"] * 100, 2) if q["iv_bid"] else None,
                        round(q["iv_ask"] * 100, 2) if q["iv_ask"] else None, round(q["delta"], 4)]
                       for q in e["quotes"]],
        } for e in expiries],
    }
    Path(out_path).write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    return data


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "autocall_market.json"))
    a = ap.parse_args()
    d = build(a.out)
    s = d["ssvi"]
    print(f"autocall_market : spot {d['spot']:,.0f} · {len(d['expiries'])} échéances · {s['n_quotes']} cotations · "
          f"RMSE SSVI {s['rmse_volpts']} pt de vol")
    for e in d["expiries"]:
        print(f"  {e['expiry'][:10]}  t {e['t']*365:6.1f} j  F {e['F']:>9,.0f}  ATM {math.sqrt(e['theta']/e['t'])*100:5.1f} %  "
              f"ρ {e['rho']:+.2f}  η {e['eta']:.2f}  RMSE {e['rmse_volpts']} pt  ({len(e['quotes'])} cot.)")
