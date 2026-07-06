"""
Réconciliation portefeuille réel Deribit ↔ état interne (positions.json).

Lit les positions réelles via l'API privée Deribit (clé READ-ONLY suffisante)
et les compare à l'état suivi par le bot. Écrit reconcile.json consommé par
generate_html.py (card « Réconciliation Deribit »).

Secrets requis (env) : DERIBIT_CLIENT_ID, DERIBIT_CLIENT_SECRET
  → créer une clé API sur deribit.com (ou test.deribit.com) avec le scope
    account:read + trade:read uniquement. Sans les secrets, le script sort
    silencieusement (exit 0) : le mode paper actuel n'est pas impacté.

Optionnel : DERIBIT_ENV=test pour pointer sur le testnet.

Usage : python deribit_reconcile.py
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE     = Path(__file__).parent
CURRENCY = "BTC"

CLIENT_ID     = os.environ.get("DERIBIT_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("DERIBIT_CLIENT_SECRET", "").strip()
BASE = ("https://test.deribit.com/api/v2" if os.environ.get("DERIBIT_ENV") == "test"
        else "https://www.deribit.com/api/v2")

TOL_CONTRACTS = 0.005   # écart de taille toléré avant flag (arrondis Deribit)


def _get(session: requests.Session, method: str, params: dict, token: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = session.get(f"{BASE}/{method}", params=params, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"{method}: {data['error']}")
    return data["result"]


def authenticate(session: requests.Session) -> str:
    res = _get(session, "public/auth", {
        "grant_type":    "client_credentials",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })
    return res["access_token"]


def fetch_real_state(session: requests.Session, token: str) -> dict:
    options = _get(session, "private/get_positions",
                   {"currency": CURRENCY, "kind": "option"}, token)
    futures = _get(session, "private/get_positions",
                   {"currency": CURRENCY, "kind": "future"}, token)
    account = _get(session, "private/get_account_summary",
                   {"currency": CURRENCY}, token)
    return {"options": options, "futures": futures, "account": account}


def reconcile(real: dict, state: dict, spot: float) -> dict:
    """Compare positions réelles et état interne. Retourne le rapport."""
    # État interne : contrats par instrument (bot = short puts → taille positive en interne)
    internal: dict[str, float] = {}
    for p in state.get("positions", []):
        name = p.get("instrument_name", "")
        internal[name] = internal.get(name, 0.0) + float(p.get("contracts", 0))

    # Réel : Deribit renvoie size en BTC, négatif pour un short
    rows, matched, mismatched = [], 0, 0
    real_by_name = {}
    for o in real["options"]:
        if abs(float(o.get("size", 0))) < 1e-9:
            continue
        real_by_name[o["instrument_name"]] = o

    for name in sorted(set(internal) | set(real_by_name)):
        int_qty  = internal.get(name, 0.0)
        o        = real_by_name.get(name)
        # short put réel = size négatif → comparé à la taille interne positive
        real_qty = -float(o["size"]) if o else 0.0
        diff     = real_qty - int_qty
        status   = "ok" if abs(diff) <= TOL_CONTRACTS else ("missing_real" if not o else
                   ("missing_internal" if int_qty == 0 else "size_mismatch"))
        if status == "ok":
            matched += 1
        else:
            mismatched += 1
        rows.append({
            "instrument":   name,
            "internal_qty": round(int_qty, 4),
            "real_qty":     round(real_qty, 4),
            "diff":         round(diff, 4),
            "status":       status,
            "mark_price":   o.get("mark_price") if o else None,
            "avg_price":    o.get("average_price") if o else None,
            "floating_pnl": o.get("floating_profit_loss") if o else None,
        })

    # Hedge : position BTC-PERPETUAL réelle vs hedge interne
    hedge_int = float(state.get("hedge", {}).get("qty", 0.0))
    perp = next((f for f in real["futures"]
                 if f.get("instrument_name") == f"{CURRENCY}-PERPETUAL"), None)
    # hedge interne stocké négatif = short ; Deribit size en USD → size_currency en BTC
    hedge_real = float(perp.get("size_currency", 0)) if perp else 0.0
    hedge_diff = hedge_real - hedge_int

    acct = real["account"]
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "env":          "testnet" if "test." in BASE else "mainnet",
        "positions":    rows,
        "n_matched":    matched,
        "n_mismatched": mismatched,
        "hedge": {
            "internal_qty": round(hedge_int, 5),
            "real_qty":     round(hedge_real, 5),
            "diff":         round(hedge_diff, 5),
            "ok":           abs(hedge_diff) <= TOL_CONTRACTS,
        },
        "account": {
            "equity_btc":            acct.get("equity"),
            "equity_usd":            round(float(acct.get("equity", 0)) * spot, 2),
            "available_margin_btc":  acct.get("available_funds"),
            "initial_margin_btc":    acct.get("initial_margin"),
            "maintenance_margin_btc": acct.get("maintenance_margin"),
        },
    }


def main() -> int:
    if not CLIENT_ID or not CLIENT_SECRET:
        print("[reconcile] DERIBIT_CLIENT_ID/SECRET absents -- mode paper, rien a faire.")
        return 0

    state = json.loads((HERE / "positions.json").read_text())

    session = requests.Session()
    token = authenticate(session)
    real  = fetch_real_state(session, token)
    spot  = float(_get(session, "public/get_index_price",
                       {"index_name": f"{CURRENCY.lower()}_usd"})["index_price"])

    report = reconcile(real, state, spot)
    (HERE / "reconcile.json").write_text(json.dumps(report, indent=2, default=str))

    print(f"[reconcile] {report['env']} — {report['n_matched']} OK, "
          f"{report['n_mismatched']} ecart(s), hedge {'OK' if report['hedge']['ok'] else 'ECART'}")
    for r in report["positions"]:
        if r["status"] != "ok":
            print(f"  [!] {r['instrument']}: interne {r['internal_qty']} vs reel {r['real_qty']} ({r['status']})")
    if not report["hedge"]["ok"]:
        h = report["hedge"]
        print(f"  [!] hedge: interne {h['internal_qty']} vs reel {h['real_qty']} BTC")
    return 0


if __name__ == "__main__":
    sys.exit(main())
