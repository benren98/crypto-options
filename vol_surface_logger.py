"""
vol_surface_logger.py — Collecte forward de la VRAIE surface de volatilité.

But : accumuler un historique réel (smile put + ATM par échéance) pour, plus tard :
  • rejouer le backtest sur données réelles au lieu du modèle BS+skew linéaire
    (suppression du risque modèle sur la période enregistrée),
  • fitter notre propre modèle de skew/term-structure et le projeter dans le passé.

À chaque exécution (1 snapshot / jour UTC, dédupliqué), pour les échéances 4-35j :
on relève par strike OTM put : mark_iv, bid_iv, delta, gamma, prix mark/bid/ask,
open interest et volume 24h. Un seul appel get_book_summary fournit volume+OI ;
le ticker par instrument fournit IV + greeks.

Append d'une ligne JSON dans vol_surface.jsonl. Branché sur GitHub Actions.

Usage : python vol_surface_logger.py
"""
import json, os, sys
from datetime import datetime, timezone
sys.path.insert(0, '.')
from greeks_hedge import get, now_ms, now_dt, fetch_spot, CURRENCY

LOG_FILE   = "vol_surface.jsonl"
DTE_MIN    = 4         # échéances retenues (jours)
DTE_MAX    = 35
MNY_MIN    = 0.55      # bande de moneyness (strike/spot) : OTM puts + un peu d'ATM
MNY_MAX    = 1.03
MAX_EXP    = 3         # nb d'échéances retenues (les plus proches)
MAX_STRIKES_PER_EXP = 24


def _dvol(currency: str):
    try:
        end = now_ms(); start = end - 6 * 3600 * 1000
        dv = get("get_volatility_index_data", {
            "currency": currency, "start_timestamp": start,
            "end_timestamp": end, "resolution": "3600"})
        rows = [r[4] for r in dv.get("data", []) if r[4]]
        return round(rows[-1], 2) if rows else None
    except Exception:
        return None


def _already_logged_today() -> bool:
    if not os.path.exists(LOG_FILE):
        return False
    today = datetime.now(timezone.utc).date().isoformat()
    last = None
    with open(LOG_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                last = line
    if not last:
        return False
    try:
        return json.loads(last).get("date") == today
    except Exception:
        return False


def run():
    if _already_logged_today():
        print("vol_surface_logger : snapshot du jour déjà enregistré, skip.")
        return

    spot = fetch_spot(CURRENCY)
    now_t = now_ms()
    instruments = get("get_instruments", {"currency": CURRENCY, "kind": "option", "expired": "false"})

    # volume + OI en un appel (book summary), mappés par instrument
    vol_oi = {}
    try:
        bs = get("get_book_summary_by_currency", {"currency": CURRENCY, "kind": "option"})
        for r in bs:
            vol_oi[r["instrument_name"]] = (r.get("volume"), r.get("open_interest"))
    except Exception:
        pass

    # filtrer puts OTM dans la fenêtre DTE + moneyness
    puts = []
    for ins in instruments:
        if ins.get("option_type") != "put":
            continue
        dte = (ins["expiration_timestamp"] - now_t) / 86400000
        if not (DTE_MIN <= dte <= DTE_MAX):
            continue
        mny = ins["strike"] / spot
        if not (MNY_MIN <= mny <= MNY_MAX):
            continue
        puts.append({"name": ins["instrument_name"], "strike": ins["strike"],
                     "exp_ts": ins["expiration_timestamp"], "dte": round(dte, 2), "mny": mny})

    # garder les MAX_EXP échéances les plus proches
    exps = sorted(set(p["exp_ts"] for p in puts))[:MAX_EXP]
    surface = []
    for exp_ts in exps:
        legs = sorted((p for p in puts if p["exp_ts"] == exp_ts), key=lambda p: abs(p["mny"] - 1))
        legs = legs[:MAX_STRIKES_PER_EXP]
        strikes = []
        for p in legs:
            try:
                t = get("ticker", {"instrument_name": p["name"]})
            except Exception:
                continue
            g = t.get("greeks", {}) or {}
            vol, oi = vol_oi.get(p["name"], (None, None))
            strikes.append({
                "strike":    p["strike"],
                "moneyness": round(p["mny"], 4),
                "mark_iv":   t.get("mark_iv"),
                "bid_iv":    t.get("bid_iv"),
                "delta":     round(g.get("delta", 0), 4) if g.get("delta") is not None else None,
                "gamma":     g.get("gamma"),
                "mark":      t.get("mark_price"),
                "bid":       t.get("best_bid_price"),
                "ask":       t.get("best_ask_price"),
                "oi":        oi if oi is not None else t.get("open_interest"),
                "vol24h":    vol,
            })
        if not strikes:
            continue
        atm = min(strikes, key=lambda s: abs(s["moneyness"] - 1))
        dte = round((exp_ts - now_t) / 86400000, 2)
        surface.append({
            "expiry": datetime.fromtimestamp(exp_ts/1000, tz=timezone.utc).date().isoformat(),
            "dte":    dte,
            "atm_iv": atm.get("mark_iv"),
            "strikes": sorted(strikes, key=lambda s: s["strike"]),
        })

    if not surface:
        print("vol_surface_logger : aucune option dans la fenêtre, rien à logger.")
        return

    snap = {
        "ts":       now_dt(),
        "date":     datetime.now(timezone.utc).date().isoformat(),
        "currency": CURRENCY,
        "spot":     round(spot, 2),
        "dvol":     _dvol(CURRENCY),
        "expiries": surface,
    }
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")

    n_lines = sum(1 for _ in open(LOG_FILE, encoding="utf-8"))
    n_strikes = sum(len(e["strikes"]) for e in surface)
    print(f"vol_surface_logger : {len(surface)} échéances · {n_strikes} strikes → {LOG_FILE} "
          f"({n_lines} jours cumulés)")


if __name__ == "__main__":
    run()
