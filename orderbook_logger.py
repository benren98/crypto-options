"""
orderbook_logger.py — B1 : collecte forward de microstructure (order book + OI + volume).

Non backtestable (Deribit ne fournit aucun historique de profondeur / OI / volume par
instrument) → on logge un snapshot à chaque run pour accumuler un dataset, puis on
analysera (orderbook_analysis.py) après quelques semaines : pinning par OI,
imbalance du carnet, filtre de liquidité conscient de la profondeur.

À chaque exécution : on scanne les meilleurs candidats (même scoring que le live),
on récupère le carnet à 10 niveaux + l'open interest + le volume 24h par instrument,
et on append UNE ligne JSON (un snapshot, tous candidats) dans orderbook_log.jsonl.

Branché sur la cadence GitHub Actions (à appeler après le pipeline principal).

Usage : python orderbook_logger.py
"""
import json, os, sys
sys.path.insert(0, '.')
from greeks_hedge import (get, now_ms, now_dt, fetch_spot, get_market_context,
                          fetch_scored_candidates, CURRENCY)

LOG_FILE = "orderbook_log.jsonl"
TOP_N    = 15      # nb de candidats snapshotés par run
DEPTH    = 10      # niveaux de carnet


def _depth_stats(levels):
    """levels = [[price, amount], ...]. Retourne (nb niveaux, taille totale 10 niveaux,
    taille des 3 premiers niveaux). Les prix d'option s'étalent trop pour une bande en %
    du mid → on mesure la profondeur exécutable sur les premiers niveaux."""
    if not levels:
        return 0, 0.0, 0.0
    total = sum(float(a) for _, a in levels)
    top3  = sum(float(a) for _, a in levels[:3])
    return len(levels), round(total, 2), round(top3, 2)


def snapshot_instrument(name: str) -> dict:
    """Carnet + OI + volume pour un instrument. get_order_book renvoie tout d'un coup."""
    ob = get("get_order_book", {"instrument_name": name, "depth": DEPTH})
    bids = ob.get("bids", []) or []
    asks = ob.get("asks", []) or []
    bb = ob.get("best_bid_price") or (float(bids[0][0]) if bids else None)
    ba = ob.get("best_ask_price") or (float(asks[0][0]) if asks else None)
    nb, bid_tot, bid_top3 = _depth_stats(bids)
    na, ask_tot, ask_top3 = _depth_stats(asks)
    imb = None
    if bid_tot + ask_tot > 0:
        imb = round((bid_tot - ask_tot) / (bid_tot + ask_tot), 3)   # >0 = pression acheteuse
    stats = ob.get("stats", {}) or {}
    return {
        "oi":          ob.get("open_interest"),
        "volume_24h":  stats.get("volume"),
        "mark_price":  ob.get("mark_price"),
        "best_bid":    bb,
        "best_ask":    ba,
        "n_bid_lvl":   nb,
        "n_ask_lvl":   na,
        "bid_depth":   bid_tot,        # contrats côté bid (10 niveaux)
        "ask_depth":   ask_tot,        # contrats côté ask
        "bid_depth_top3": bid_top3,    # liquidité exécutable (3 premiers niveaux)
        "ask_depth_top3": ask_top3,
        "imbalance":   imb,            # déséquilibre du carnet [-1, +1]
        "bids_top3":   [[float(p), float(a)] for p, a in bids[:3]],
        "asks_top3":   [[float(p), float(a)] for p, a in asks[:3]],
    }


def run():
    ctx  = get_market_context(CURRENCY)
    spot = fetch_spot(CURRENCY)
    cands = fetch_scored_candidates(
        CURRENCY, spot, ctx["hv_blend"], ctx["iv_min"], ctx["iv_max"], ctx["curr_iv"])
    if cands.empty:
        print("orderbook_logger : aucun candidat, rien à logger.")
        return

    rows = []
    for _, r in cands.head(TOP_N).iterrows():
        name = r["instrument_name"]
        rec = {
            "instrument": name,
            "strike":     float(r.get("strike", 0)),
            "expiry":     str(r.get("expiry_dt", ""))[:10],
            "tte_days":   round(float(r.get("tte_days", 0)), 2),
            "moneyness":  round(float(r.get("moneyness", 0)), 2),
            "delta":      round(float(r.get("delta", 0)), 4),
            "gamma":      float(r.get("gamma", 0)),
            "score":      float(r.get("score", 0)),
            "bid_iv":     float(r.get("bid_iv", 0)),
            "ba_pct":     round(float(r.get("ba_pct", 0)), 2),
        }
        try:
            rec.update(snapshot_instrument(name))
        except Exception as e:
            rec["error"] = str(e)[:120]
        rows.append(rec)

    snapshot = {
        "ts":        now_dt(),
        "ts_ms":     now_ms(),
        "currency":  CURRENCY,
        "spot":      round(spot, 2),
        "dvol":      round(float(ctx.get("curr_iv", 0)), 2),
        "hv_blend":  round(float(ctx.get("hv_blend", 0)), 2),
        "regime":    ctx.get("regime"),
        "candidates": rows,
    }
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")

    n_lines = sum(1 for _ in open(LOG_FILE, encoding="utf-8")) if os.path.exists(LOG_FILE) else 0
    print(f"orderbook_logger : {len(rows)} candidats snapshotés → {LOG_FILE} ({n_lines} snapshots cumulés)")


if __name__ == "__main__":
    run()
