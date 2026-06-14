"""
orderbook_analysis.py — B2 : analyse du dataset de microstructure accumulé par
orderbook_logger.py. À lancer une fois quelques semaines de snapshots collectées.

Trois axes (cf plan) :
  1. OI / volume par strike   → pinning & support/résistance
  2. Imbalance du carnet       → signal directionnel court-terme
  3. Profondeur                → filtre de liquidité conscient de la taille
     (remplacer BA_MAX_PCT top-of-book par une capacité réelle du carnet)

Ce script fait l'analyse DESCRIPTIVE possible sans données d'issue, et prépare
les jointures avec le spot futur (pinning, signal) une fois l'historique assez long.

Usage : python orderbook_analysis.py
"""
import json, sys, statistics as st
from collections import defaultdict

LOG_FILE = "orderbook_log.jsonl"

def load():
    snaps = []
    try:
        for line in open(LOG_FILE, encoding="utf-8"):
            line = line.strip()
            if line:
                snaps.append(json.loads(line))
    except FileNotFoundError:
        print(f"{LOG_FILE} introuvable — lance d'abord orderbook_logger.py (et laisse Actions accumuler).")
        sys.exit(0)
    return snaps

def pctl(xs, p):
    xs = sorted(x for x in xs if x is not None)
    return xs[min(len(xs)-1, int(len(xs)*p))] if xs else None

def main():
    snaps = load()
    cand_rows = [(s, c) for s in snaps for c in s.get("candidates", [])]
    print(f"\n  DATASET order-book : {len(snaps)} snapshots · {len(cand_rows)} lignes candidat")
    if snaps:
        print(f"  Période : {snaps[0]['ts']}  →  {snaps[-1]['ts']}")
    if len(snaps) < 20:
        print(f"  ⚠ Trop peu de snapshots ({len(snaps)}) pour des conclusions — laisse accumuler "
              f"(~1 / heure via Actions, vise >300 ≈ 2 semaines).")

    # ── Axe 3 : descriptif liquidité / OI / volume / imbalance ──
    def col(key): return [c.get(key) for _, c in cand_rows if c.get(key) is not None]
    print(f"\n  ── Descriptif (tous candidats) ──")
    for key, lab in [("oi","OI"),("volume_24h","Vol 24h"),("bid_depth","Prof. bid (10niv)"),
                     ("ask_depth","Prof. ask (10niv)"),("ba_pct","B/A %"),("imbalance","Imbalance")]:
        xs = col(key)
        if xs:
            print(f"    {lab:<20} médiane {pctl(xs,0.5):>8.2f}  p10 {pctl(xs,0.1):>8.2f}  p90 {pctl(xs,0.9):>8.2f}")

    # ── Axe 1 : OI agrégé par strike (pinning) ──
    oi_by_strike = defaultdict(list)
    for _, c in cand_rows:
        if c.get("oi") is not None:
            oi_by_strike[c.get("strike")].append(c["oi"])
    if oi_by_strike:
        print(f"\n  ── OI moyen par strike (top 8) — aimants potentiels ──")
        top = sorted(((k, st.mean(v)) for k, v in oi_by_strike.items()), key=lambda x: -x[1])[:8]
        for strike, oi in top:
            print(f"    strike {strike:>10,.0f} : OI moyen {oi:>10,.0f}")
        print(f"  (pinning : à corréler avec le spot à l'expiration une fois l'historique assez long)")

    # ── Axe 2 : imbalance — distribution (signal directionnel, à valider sur le move futur) ──
    imbs = col("imbalance")
    if imbs:
        pos = sum(1 for x in imbs if x > 0.1); neg = sum(1 for x in imbs if x < -0.1)
        print(f"\n  ── Imbalance carnet ── {len(imbs)} obs · pression acheteuse {pos} / vendeuse {neg}")
        print(f"  (signal : joindre imbalance(t) au move spot(t→t+1) pour mesurer le pouvoir prédictif)")

    print(f"\n  Prochaines étapes (quand l'historique le permet) :")
    print(f"   • Pinning   : spot à l'expiration vs strikes à fort OI")
    print(f"   • Signal    : corrélation imbalance → move spot suivant")
    print(f"   • Liquidité : profondeur exécutable vs notre taille cible → filtre remplaçant BA_MAX_PCT")

if __name__ == "__main__":
    main()
