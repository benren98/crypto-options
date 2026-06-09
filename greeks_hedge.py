"""
Greeks Engine, Delta Hedge & Roll Manager
==========================================
1. Calcule les Greeks (Black-Scholes) et les compare aux Greeks Deribit.
2. Gère une position de short puts OTM avec rolling automatique :
     - Entrée  : TTE >= MIN_TTE_ENTRY (2j)
     - Roll    : TTE <= ROLL_TRIGGER  (1j) -> ferme + réouvre
3. Calcule le hedge delta via perp futures et génère les ordres.
4. Suit le PnL mark-to-market en temps réel.

Usage:
    python greeks_hedge.py --run          # scan + affiche position + hedge
    python greeks_hedge.py --monitor      # boucle toutes les N minutes
    python greeks_hedge.py --backtest     # simule les rolls sur données historiques
"""

import argparse
import json
import math
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

from gist_sync import push_positions

import numpy as np
import pandas as pd
import requests
from scipy.stats import norm
from urllib3.exceptions import InsecureRequestWarning

warnings.filterwarnings("ignore", category=InsecureRequestWarning)

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL        = "https://www.deribit.com/api/v2/public"
OUTPUT_DIR      = Path(__file__).parent / "output"
POSITIONS_FILE  = Path(__file__).parent / "positions.json"
OUTPUT_DIR.mkdir(exist_ok=True)

CURRENCY        = "BTC"          # BTC | ETH
DELTA_TARGET    = -0.20          # delta cible pour le put vendu
DELTA_TOL       = 0.06           # tolérance autour du delta cible
MIN_TTE_ENTRY   = 2.0            # jours minimum pour entrer
MAX_TTE_ENTRY   = 7.0            # jours maximum pour entrer
ROLL_TRIGGER         = 1.0   # jours restants -> fenêtre d'observation pour le roll
GAMMA_ROLL_THRESHOLD = 6.0   # pts de delta / 1% move AU-DESSUS duquel on rolle
                              # (roll si TTE <= ROLL_TRIGGER ET gamma > seuil)
                              # Gamma > 6pts = option se rapproche d'ATM = danger
HEDGE_THRESHOLD_BASE_PCT = 5.0   # bande de base en % de delta (ex : 5% = rebalance si drift > 5pts Δ)
HEDGE_IV_REF         = 70.0      # IV de référence BTC "normale" — calibre la bande
# Formule : threshold_pct = BASE × sqrt(IV_current / IV_REF), clampé [2%, 8%]
# Plus la vol est élevée → bandes plus larges → moins de rebalancements inutiles
RISK_FREE_RATE  = 0.05           # taux sans risque annualisé (approx)
CONTRACTS       = 1              # nombre de puts vendus (1 contrat = 1 BTC sur Deribit)


# ── Helpers API ───────────────────────────────────────────────────────────────

def get(method: str, params: dict) -> dict:
    r = requests.get(f"{BASE_URL}/{method}", params=params,
                     timeout=15, verify=False)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"API error: {data['error']}")
    return data["result"]


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def now_dt() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ── Black-Scholes Greeks ──────────────────────────────────────────────────────

class BSGreeks:
    """
    Calcule tous les Greeks BS pour une option européenne.
    sigma : vol annualisée (ex: 0.70 pour 70%)
    T     : temps à l'expiration en années
    """

    def __init__(self, S: float, K: float, T: float, r: float,
                 sigma: float, option_type: str = "put"):
        self.S    = S
        self.K    = K
        self.T    = max(T, 1e-6)   # éviter division par zéro
        self.r    = r
        self.sigma= sigma
        self.q    = 0.0            # pas de dividende
        self.type = option_type.lower()

        sqrtT     = math.sqrt(self.T)
        self.d1   = (math.log(S / K) + (r - self.q + 0.5 * sigma**2) * self.T) \
                    / (sigma * sqrtT)
        self.d2   = self.d1 - sigma * sqrtT

    def price(self) -> float:
        S, K, T, r, q = self.S, self.K, self.T, self.r, self.q
        d1, d2 = self.d1, self.d2
        disc = math.exp(-r * T)
        if self.type == "call":
            return S * math.exp(-q*T) * norm.cdf(d1) - K * disc * norm.cdf(d2)
        else:
            return K * disc * norm.cdf(-d2) - S * math.exp(-q*T) * norm.cdf(-d1)

    def delta(self) -> float:
        e_qt = math.exp(-self.q * self.T)
        if self.type == "call":
            return e_qt * norm.cdf(self.d1)
        else:
            return e_qt * (norm.cdf(self.d1) - 1)

    def gamma(self) -> float:
        """Même valeur pour call et put."""
        phi = norm.pdf(self.d1)
        return (math.exp(-self.q * self.T) * phi) / \
               (self.S * self.sigma * math.sqrt(self.T))

    def vega(self) -> float:
        """Sensibilité à une variation de 1 point de vol (1% = 0.01)."""
        phi = norm.pdf(self.d1)
        return self.S * math.exp(-self.q * self.T) * phi * math.sqrt(self.T) / 100

    def theta(self) -> float:
        """
        Decay journalier en fraction d'unité de l'underlying.
        Sur Deribit les options BTC/ETH sont cotées en BTC/ETH (pas en USD),
        donc on divise par S pour obtenir le theta en fraction de BTC/ETH/jour.
        """
        S, K, T, r, q = self.S, self.K, self.T, self.r, self.q
        phi  = norm.pdf(self.d1)
        sqrtT= math.sqrt(T)
        term1= -(S * math.exp(-q*T) * phi * self.sigma) / (2 * sqrtT)
        if self.type == "call":
            term2 = -r * K * math.exp(-r*T) * norm.cdf(self.d2)
            term3 =  q * S * math.exp(-q*T) * norm.cdf(self.d1)
        else:
            term2 =  r * K * math.exp(-r*T) * norm.cdf(-self.d2)
            term3 = -q * S * math.exp(-q*T) * norm.cdf(-self.d1)
        theta_usd_per_day = (term1 + term2 + term3) / 365
        return theta_usd_per_day / S   # convertir en BTC/ETH par jour

    def rho(self) -> float:
        T, K, r = self.T, self.K, self.r
        if self.type == "call":
            return K * T * math.exp(-r*T) * norm.cdf(self.d2) / 100
        else:
            return -K * T * math.exp(-r*T) * norm.cdf(-self.d2) / 100

    def vanna(self) -> float:
        """dDelta/dVol — important pour OTM options."""
        phi = norm.pdf(self.d1)
        return -math.exp(-self.q * self.T) * phi * self.d2 / self.sigma

    def charm(self) -> float:
        """dDelta/dTime — vitesse de decay du delta."""
        phi  = norm.pdf(self.d1)
        sqrtT= math.sqrt(self.T)
        if self.type == "call":
            nd1 = norm.cdf(self.d1)
            return self.q * math.exp(-self.q*self.T) * nd1 \
                   - math.exp(-self.q*self.T) * phi \
                   * (2*(self.r-self.q)*self.T - self.d2*self.sigma*sqrtT) \
                   / (2*self.T*self.sigma*sqrtT)
        else:
            nd1 = norm.cdf(-self.d1)
            return -self.q * math.exp(-self.q*self.T) * nd1 \
                   - math.exp(-self.q*self.T) * phi \
                   * (2*(self.r-self.q)*self.T - self.d2*self.sigma*sqrtT) \
                   / (2*self.T*self.sigma*sqrtT)

    def summary(self) -> dict:
        return {
            "price":  round(self.price(),  6),
            "delta":  round(self.delta(),  5),
            "gamma":  round(self.gamma(),  6),
            "vega":   round(self.vega(),   4),
            "theta":  round(self.theta(),  6),
            "rho":    round(self.rho(),    4),
            "vanna":  round(self.vanna(),  5),
            "charm":  round(self.charm(),  6),
        }


# ── Market data helpers ───────────────────────────────────────────────────────

def fetch_spot(currency: str) -> float:
    data = get("get_index_price", {"index_name": f"{currency.lower()}_usd"})
    return data["index_price"]


def fetch_ticker_full(instrument_name: str) -> dict:
    return get("ticker", {"instrument_name": instrument_name})


def fetch_put_candidates(currency: str,
                          delta_target: float = DELTA_TARGET,
                          delta_tol:    float = DELTA_TOL,
                          min_tte:      float = MIN_TTE_ENTRY,
                          max_tte:      float = MAX_TTE_ENTRY) -> pd.DataFrame:
    """Retourne les puts OTM dans la fenêtre de maturité et de delta."""
    instruments = get("get_instruments", {
        "currency": currency,
        "kind": "option",
        "expired": "false",
    })
    now = now_ms()
    rows = []
    targets = [i for i in instruments
               if i["instrument_name"].endswith("-P")]

    for inst in targets:
        tte = (inst["expiration_timestamp"] - now) / 86_400_000
        if not (min_tte <= tte <= max_tte):
            continue
        try:
            t = fetch_ticker_full(inst["instrument_name"])
            greeks = t.get("greeks") or {}
            delta  = greeks.get("delta")
            if delta is None:
                continue
            if not (delta_target - delta_tol <= delta <= delta_target + delta_tol):
                continue
            rows.append({
                "instrument_name":  inst["instrument_name"],
                "strike":           inst["strike"],
                "expiry_dt":        pd.to_datetime(inst["expiration_timestamp"],
                                                   unit="ms", utc=True),
                "tte_days":         round(tte, 3),
                "delta":            delta,
                "gamma":            greeks.get("gamma"),
                "vega":             greeks.get("vega"),
                "theta":            greeks.get("theta"),
                "mark_iv":          t.get("mark_iv"),
                "mark_price":       t.get("mark_price"),
                "bid_price":        t.get("best_bid_price"),
                "ask_price":        t.get("best_ask_price"),
                "underlying_price": t.get("underlying_price"),
                "open_interest":    t.get("open_interest"),
            })
        except Exception:
            continue

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["delta_dist"] = (df["delta"] - delta_target).abs()
    df["score"] = df["delta_dist"] / df["delta_dist"].max() \
                + (df["tte_days"] - min_tte) / max(df["tte_days"].max() - min_tte, 1)
    df.sort_values("score", inplace=True)
    return df.reset_index(drop=True)


# ── Position & Roll Manager ───────────────────────────────────────────────────

def load_positions() -> dict:
    if not POSITIONS_FILE.exists():
        return {"positions": [], "hedge": {}, "history": []}
    state = json.loads(POSITIONS_FILE.read_text())
    # Migration ancien format open:dict → positions:list
    if "open" in state and "positions" not in state:
        pos = state.pop("open") or {}
        state["positions"] = [pos] if pos else []
        state["hedge"] = {
            "qty":              pos.pop("hedge_qty", 0.0),
            "avg_entry":        pos.pop("hedge_avg_entry", pos.get("hedge_entry_spot", 0.0)),
            "rebalances":       pos.pop("hedge_rebalances", 0),
            "history":          pos.pop("hedge_history", []),
            "realized_pnl_usd": pos.pop("realized_hedge_pnl_usd", 0.0),
        }
    # Compat: expose state["open"] = première position ouverte (pour roll logic legacy)
    state["open"] = state["positions"][0] if state.get("positions") else None
    return state


def save_positions(state: dict):
    POSITIONS_FILE.write_text(json.dumps(state, indent=2, default=str))


def open_position(instrument: dict, entry_price: float,
                  contracts: int, spot: float) -> dict:
    hedge_qty_init = round(-abs(instrument["delta"]) * contracts, 5)

    # Prix d'exécution du perp : on récupère le mark price du perp (≠ index spot)
    # Le perp peut coter avec une prime ou décote vs l'index → VWAP basé sur perp mark
    try:
        perp_tick  = get("ticker", {"instrument_name": f"{CURRENCY}-PERPETUAL"})
        perp_price = perp_tick.get("mark_price", spot)
    except Exception:
        perp_price = spot   # fallback si API indisponible

    return {
        "instrument_name":  instrument["instrument_name"],
        "strike":           instrument["strike"],
        "expiry_dt":        str(instrument["expiry_dt"]),
        "tte_at_entry":     instrument["tte_days"],
        "delta_at_entry":   instrument["delta"],
        "gamma_at_entry":   instrument.get("gamma", 7e-5),
        "vega_at_entry":    instrument.get("vega", 0),
        "theta_at_entry":   instrument.get("theta", 0),
        "iv_at_entry":      instrument["mark_iv"],
        "entry_price":      entry_price,
        "entry_mark_price": instrument.get("mark_price", entry_price),
        "entry_price_usd":  round(entry_price * spot, 2),
        "entry_spot":       spot,
        "contracts":        contracts,
        "entry_ts":         now_dt(),
        "hedge_qty":        hedge_qty_init,
        "hedge_entry_spot": spot,
        "hedge_avg_entry":  round(perp_price, 2),   # VWAP basé sur perp mark, pas index
        "hedge_rebalances": 1,
        "hedge_history": [{
            "ts":          now_dt(),
            "side":        "SELL",
            "qty":         hedge_qty_init,
            "spot":        round(spot, 2),
            "perp_price":  round(perp_price, 2),
            "qty_before":  0.0,
            "qty_after":   hedge_qty_init,
            "vwap_before": 0.0,
            "vwap_after":  round(perp_price, 2),
            "drift":       round(abs(instrument["delta"]), 5),
            "note":        f"hedge initial (perp mark ${perp_price:.2f} vs index ${spot:.2f})",
        }],
    }


def should_roll(position: dict, spot: float) -> tuple[bool, float, float, str]:
    """
    Vérifie si la position doit être rollée.

    Logique :
    - Si TTE > ROLL_TRIGGER : pas encore dans la fenêtre -> pas de roll
    - Si TTE <= ROLL_TRIGGER :
        - Si gamma_pts < GAMMA_ROLL_THRESHOLD -> roll (gamma s'est effondré)
        - Si l'instrument est expiré (introuvable) -> roll immédiat
        - Sinon -> attendre (gamma encore élevé, OTM saine)

    Retourne : (roll_now, tte_days, gamma_pts_per_1pct, reason)
    """
    try:
        t    = fetch_ticker_full(position["instrument_name"])
        inst = get("get_instruments", {
            "currency": CURRENCY, "kind": "option", "expired": "false"
        })
        exp_ms = next(
            (i["expiration_timestamp"] for i in inst
             if i["instrument_name"] == position["instrument_name"]),
            None
        )
        if exp_ms is None:
            return True, 0.0, 0.0, "instrument expiré"

        tte = (exp_ms - now_ms()) / 86_400_000

        # Pas encore dans la fenêtre — on calcule quand même gamma pour l'affichage
        greeks_pre  = t.get("greeks") or {}
        gamma_pre   = abs(greeks_pre.get("gamma", position.get("gamma_at_entry", 7e-5)))
        gamma_pts_pre = gamma_pre * spot * 0.01 * 100
        if tte > ROLL_TRIGGER:
            return False, tte, gamma_pts_pre, f"TTE {tte:.2f}j > seuil {ROLL_TRIGGER}j"

        # Dans la fenêtre : calculer gamma_pts
        greeks  = t.get("greeks") or {}
        gamma   = abs(greeks.get("gamma", position.get("gamma_at_entry", 7e-5)))
        gamma_pts = gamma * spot * 0.01 * 100   # pts de delta (%) perdus par 1% move

        if gamma_pts > GAMMA_ROLL_THRESHOLD:
            reason = (f"TTE {tte:.2f}j ≤ {ROLL_TRIGGER}j "
                      f"ET gamma {gamma_pts:.2f}pts > seuil {GAMMA_ROLL_THRESHOLD}pts — ATM danger")
            return True, tte, gamma_pts, reason
        else:
            reason = (f"TTE {tte:.2f}j ≤ {ROLL_TRIGGER}j "
                      f"mais gamma {gamma_pts:.2f}pts ≤ seuil {GAMMA_ROLL_THRESHOLD}pts — OTM OK, HOLD")
            return False, tte, gamma_pts, reason

    except Exception as e:
        return True, 0.0, 0.0, f"erreur: {e}"


# ── Greeks & Hedge Calculator ─────────────────────────────────────────────────

def compute_position_greeks(position: dict, spot: float) -> dict:
    """
    Calcule les Greeks BS de la position short put.
    Retourne les Greeks de la position (x contracts, signés pour short).
    """
    t = fetch_ticker_full(position["instrument_name"])
    greeks_deribit = t.get("greeks") or {}
    mark_iv   = t.get("mark_iv", 0) / 100   # Deribit donne en %
    mark_price= t.get("mark_price", 0)

    exp_ms = int(pd.to_datetime(position["expiry_dt"]).timestamp() * 1000)
    tte_years = (exp_ms - now_ms()) / (365 * 24 * 3600 * 1000)

    bs = BSGreeks(
        S=spot,
        K=position["strike"],
        T=tte_years,
        r=RISK_FREE_RATE,
        sigma=mark_iv,
        option_type="put",
    )
    bs_summary = bs.summary()
    n = position["contracts"]

    # Position SHORT : on inverse le signe des Greeks sensibles à la direction
    # theta BS est en fraction de BTC/jour (prix en BTC sur Deribit)
    # on le convertit en USD pour l'affichage
    pos_greeks = {
        "instrument":    position["instrument_name"],
        "tte_days":      round(tte_years * 365, 3),
        "mark_price":    mark_price,
        "mark_iv_pct":   t.get("mark_iv"),
        "spot":          spot,
        # Greeks de la position (short = *-1 pour delta/vega/theta)
        "pos_delta":     round(-n * bs_summary["delta"], 5),  # short put -> long delta (+)
        "pos_gamma":     round(-n * bs_summary["gamma"], 6),  # short -> short gamma (-)
        "pos_vega":      round(-n * bs_summary["vega"],  4),  # short -> short vega (-)
        "pos_theta":     round(-n * bs_summary["theta"], 6),  # short -> long theta (+, en BTC)
        "pos_charm":     round(-n * bs_summary["charm"], 6),
        "pos_vanna":     round(-n * bs_summary["vanna"], 5),
        # Greeks BS unitaires
        "bs_delta":      bs_summary["delta"],
        "bs_gamma":      bs_summary["gamma"],
        "bs_vega":       bs_summary["vega"],
        "bs_theta":      bs_summary["theta"],
        "bs_charm":      bs_summary["charm"],
        # Comparaison Deribit vs BS
        "deribit_delta": greeks_deribit.get("delta"),
        "deribit_gamma": greeks_deribit.get("gamma"),
        "deribit_vega":  greeks_deribit.get("vega"),
        "deribit_theta": greeks_deribit.get("theta"),
    }
    return pos_greeks


def compute_hedge_threshold(iv_pct: float, contracts: int = 1) -> tuple[float, float]:
    """
    Calcule la bande de rebalancement dynamique en fonction de l'IV.

    Logique :
      threshold_pct = BASE_PCT × sqrt(IV_current / IV_REF)
      clampé entre 2 % et 8 %

    Plus l'IV est élevée → moves attendus plus grands → coût d'un rebalancement
    prématuré > coût du gamma bleed → bandes plus larges.

    Retourne : (threshold_btc, threshold_pct)
      threshold_btc = fraction de l'underlying à dépasser pour déclencher un ordre
      threshold_pct = en points de delta (%, lisible)
    """
    iv_scale      = math.sqrt(max(iv_pct, 20.0) / HEDGE_IV_REF)
    threshold_pct = HEDGE_THRESHOLD_BASE_PCT * iv_scale
    threshold_pct = max(2.0, min(8.0, threshold_pct))   # bornes de sécurité
    threshold_btc = threshold_pct / 100.0 * contracts
    return round(threshold_btc, 5), round(threshold_pct, 2)


def compute_hedge_order(pos_greeks: dict, current_hedge_qty: float,
                         spot: float) -> dict:
    """
    Calcule l'ordre de rebalancement du hedge delta via perp.

    Convention Deribit :
      - Delta d'un put = négatif (ex: -0.20)
      - Short put -> position delta = +0.20 (long delta)
      - Pour neutraliser : shorter 0.20 BTC de perp
      - hedge_qty stocké en positif = short perp
    """
    target_hedge  = -pos_greeks["pos_delta"]   # qty à shorter sur perp
    delta_drift   = target_hedge - current_hedge_qty

    # Seuil dynamique basé sur l'IV courante
    iv_pct = pos_greeks.get("mark_iv_pct") or HEDGE_IV_REF
    threshold_btc, threshold_pct = compute_hedge_threshold(iv_pct, contracts=1)
    needs_rebalance = abs(delta_drift) > threshold_btc

    # target_hedge > 0 = on doit shorter du perp
    # order: si target > current -> SELL perp (augmenter le short)
    #        si target < current -> BUY  perp (réduire le short)
    return {
        "current_hedge_qty":   round(current_hedge_qty, 5),
        "target_hedge_qty":    round(target_hedge, 5),
        "delta_drift":         round(delta_drift, 5),
        "needs_rebalance":     needs_rebalance,
        "order_qty":           round(abs(delta_drift), 5) if needs_rebalance else 0.0,
        "order_side":          "sell" if delta_drift > 0 else "buy",
        "order_value_usd":     round(abs(delta_drift) * spot, 2),
        "hedge_threshold_btc": threshold_btc,
        "hedge_threshold_pct": threshold_pct,
    }


def compute_pnl(position: dict, spot: float) -> dict:
    """PnL mark-to-market de la position short put."""
    t = fetch_ticker_full(position["instrument_name"])
    current_price = t.get("mark_price", 0)
    entry_price   = position["entry_price"]
    n             = position["contracts"]

    # Short put PnL : on a encaissé entry_price, on doit potentiellement racheter
    # PnL en BTC puis converti en USD
    pnl_btc   = (entry_price - current_price) * n
    pnl_usd   = pnl_btc * spot
    pnl_pct   = (pnl_btc / entry_price) * 100 if entry_price > 0 else 0

    # Hedge PnL (perp short)
    hedge_qty     = position.get("hedge_qty", 0)
    entry_spot    = position.get("entry_spot", spot)
    hedge_pnl_usd = hedge_qty * (entry_spot - spot)  # short = gagne si spot baisse

    return {
        "entry_price":     entry_price,
        "current_price":   current_price,
        "pnl_btc":         round(pnl_btc, 6),
        "pnl_usd":         round(pnl_usd, 2),
        "pnl_pct":         round(pnl_pct, 2),
        "hedge_pnl_usd":   round(hedge_pnl_usd, 2),
        "total_pnl_usd":   round(pnl_usd + hedge_pnl_usd, 2),
        # pos_theta est en BTC/jour -> convertir en USD
        "theta_daily_usd": round(pos_greeks_cache.get("pos_theta", 0) * spot, 2)
                           if "pos_theta" in pos_greeks_cache else 0.0,
    }


# Cache pour éviter double appel API
pos_greeks_cache: dict = {}


# ── Display ───────────────────────────────────────────────────────────────────

def print_separator(char="=", width=62):
    print(char * width)


def print_section(title: str):
    print(f"\n{'─'*62}")
    print(f"  {title}")
    print(f"{'─'*62}")


def display_greeks(g: dict):
    print(f"  Instrument   : {g['instrument']}")
    print(f"  TTE          : {g['tte_days']:.2f} jours")
    print(f"  Spot         : ${g['spot']:,.2f}")
    print(f"  Mark price   : {g['mark_price']:.5f} BTC  |  IV: {g['mark_iv_pct']:.1f}%")
    print()
    print(f"  {'Greek':<10} {'BS calc':>12} {'Deribit':>12} {'Position':>12}")
    print(f"  {'-'*50}")
    greeks_list = [
        ("Delta",  "bs_delta",  "deribit_delta", "pos_delta"),
        ("Gamma",  "bs_gamma",  "deribit_gamma", "pos_gamma"),
        ("Vega",   "bs_vega",   "deribit_vega",  "pos_vega"),
        ("Theta",  "bs_theta",  "deribit_theta", "pos_theta"),
        ("Charm",  "bs_charm",  None,            "pos_charm"),
        ("Vanna",  None,        None,            "pos_vanna"),
    ]
    for name, bs_key, der_key, pos_key in greeks_list:
        bs_val  = f"{g[bs_key]:>12.5f}"  if bs_key  and g.get(bs_key)  is not None else f"{'N/A':>12}"
        der_val = f"{g[der_key]:>12.5f}" if der_key and g.get(der_key) is not None else f"{'N/A':>12}"
        pos_val = f"{g[pos_key]:>12.5f}" if pos_key and g.get(pos_key) is not None else f"{'N/A':>12}"
        print(f"  {name:<10} {bs_val} {der_val} {pos_val}")


def display_hedge(h: dict):
    thr_btc = h.get("hedge_threshold_btc", 0.03)
    thr_pct = h.get("hedge_threshold_pct", 3.0)
    drift_pct = abs(h["delta_drift"]) * 100
    status = ">> REBALANCEMENT REQUIS <<" if h["needs_rebalance"] else f"OK (drift {drift_pct:.2f}% < seuil {thr_pct:.1f}%)"
    print(f"  Hedge actuel   : {h['current_hedge_qty']:+.5f} BTC short perp")
    print(f"  Hedge cible    : {h['target_hedge_qty']:+.5f} BTC short perp")
    print(f"  Drift delta    : {h['delta_drift']:+.5f} ({drift_pct:.2f}%)  [{status}]")
    print(f"  Seuil IV-adj   : {thr_pct:.1f}% delta = {thr_btc:.5f} BTC")
    if h["needs_rebalance"]:
        print(f"\n  *** ORDRE HEDGE ***")
        print(f"  Action  : {h['order_side'].upper()} {abs(h['order_qty']):.5f} BTC-PERPETUAL")
        print(f"  Valeur  : ${h['order_value_usd']:,.2f}")


def display_pnl(p: dict):
    sign = "+" if p["total_pnl_usd"] >= 0 else ""
    print(f"  PnL option     : {sign}{p['pnl_usd']:,.2f} USD  ({sign}{p['pnl_pct']:.2f}%)")
    print(f"  PnL hedge      : {'+' if p['hedge_pnl_usd']>=0 else ''}{p['hedge_pnl_usd']:,.2f} USD")
    print(f"  PnL total      : {sign}{p['total_pnl_usd']:,.2f} USD")
    print(f"  Theta daily    : +{p['theta_daily_usd']:,.2f} USD/jour (attendu)")


# ── Main run ──────────────────────────────────────────────────────────────────

def run_once(currency: str = CURRENCY, verbose: bool = True):
    global pos_greeks_cache

    print_separator()
    print(f"  Greeks & Hedge Engine — {now_dt()}")
    print(f"  Currency: {currency}  |  Contracts: {CONTRACTS}")
    print_separator()

    spot = fetch_spot(currency)
    print(f"\n  Spot {currency}: ${spot:,.2f}")

    state = load_positions()

    # ── Vérifier si roll nécessaire ───────────────────────────────────────────
    if state["open"] is not None:
        roll_needed, tte_current, gamma_pts_now, roll_reason = should_roll(state["open"], spot)

        print_section("ROLL CHECK")
        print(f"  TTE actuel     : {tte_current:.3f}j  (seuil entrée fenêtre : {ROLL_TRIGGER}j)")
        print(f"  Gamma actuel   : {gamma_pts_now:.2f} pts Δ/1%  (seuil roll : {GAMMA_ROLL_THRESHOLD} pts)")
        print(f"  Décision       : {'🔴 ROLL' if roll_needed else '🟢 HOLD'}  — {roll_reason}")

        if roll_needed:
            print_section("ROLL TRIGGERED")
            pos    = state["open"]
            t      = fetch_ticker_full(pos["instrument_name"])
            exit_p = t.get("mark_price", pos["entry_price"])

            # Clôture de la position existante
            closed = {**pos,
                      "exit_price":   exit_p,
                      "exit_spot":    spot,
                      "exit_ts":      now_dt(),
                      "tte_at_exit":  round(tte_current, 3),
                      "gamma_at_exit":round(gamma_pts_now, 4),
                      "roll_reason":  roll_reason,
                      "pnl_btc":      round((pos["entry_price"] - exit_p) * CONTRACTS, 6),
                      "pnl_usd":      round((pos["entry_price"] - exit_p) * CONTRACTS * spot, 2)}
            state["history"].append(closed)
            state["open"] = None
            print(f"  Position fermee : {pos['instrument_name']}")
            print(f"  PnL realise     : {closed['pnl_btc']:+.6f} BTC  "
                  f"({'+' if closed['pnl_usd']>=0 else ''}{closed['pnl_usd']:.2f} USD)")

    # ── Ouvrir une nouvelle position si nécessaire ────────────────────────────
    if state["open"] is None:
        print_section("RECHERCHE CANDIDAT SHORT PUT")
        print(f"  Criteres: TTE [{MIN_TTE_ENTRY:.0f}j - {MAX_TTE_ENTRY:.0f}j]"
              f"  |  delta cible: {DELTA_TARGET:+.2f} +/- {DELTA_TOL:.2f}")

        candidates = fetch_put_candidates(currency)
        if candidates.empty:
            print("  Aucun candidat trouve. Reessayer plus tard.")
            save_positions(state)
            return

        best = candidates.iloc[0]
        # On vend un put : on reçoit le BID (le market maker achète au bid)
        # Fallback sur mark si bid absent (illiquidité)
        entry_price = best["bid_price"] or best["mark_price"]
        state["open"] = open_position(best, entry_price, CONTRACTS, spot)

        print(f"\n  Meilleur candidat:")
        print(f"  {best['instrument_name']}")
        print(f"    Strike     : {best['strike']:,.0f}  ({(best['strike']/spot-1)*100:+.1f}% moneyness)")
        print(f"    TTE        : {best['tte_days']:.2f} jours")
        print(f"    Delta      : {best['delta']:+.4f}")
        print(f"    IV         : {best['mark_iv']:.1f}%")
        print(f"    Prix entry : {entry_price:.5f} BTC  (${entry_price*spot:,.2f})")
        print(f"    OI         : {best['open_interest']:.1f} contracts")

        if verbose and len(candidates) > 1:
            print(f"\n  Autres candidats:")
            for _, row in candidates.head(5).iterrows():
                print(f"    {row['instrument_name']:<30}  "
                      f"TTE={row['tte_days']:.1f}j  "
                      f"delta={row['delta']:+.3f}  "
                      f"IV={row['mark_iv']:.1f}%")

    # ── Greeks de TOUTES les positions (cumulés) ──────────────────────────────
    open_positions = state.get("positions", [])
    if not open_positions:
        open_positions = [state["open"]] if state.get("open") else []

    print_section(f"GREEKS PORTEFEUILLE ({len(open_positions)} position(s))")
    all_greeks = [compute_position_greeks(p, spot) for p in open_positions]
    for pg in all_greeks:
        display_greeks(pg)

    # Greeks cumulés pour le hedge
    combined_greeks = {
        "pos_delta": sum(g["pos_delta"] for g in all_greeks),
        "pos_gamma": sum(g["pos_gamma"] for g in all_greeks),
        "pos_vega":  sum(g["pos_vega"]  for g in all_greeks),
        "pos_theta": sum(g["pos_theta"] for g in all_greeks),
        "mark_iv_pct": max(g.get("mark_iv_pct") or 50 for g in all_greeks),
        "spot":      spot,
    }
    pos_greeks = combined_greeks
    pos_greeks_cache = combined_greeks

    # ── Hedge delta (sur delta cumulé portfolio) ───────────────────────────────
    hedge_data = state.get("hedge", {})
    current_hedge_qty = float(hedge_data.get("qty", 0.0))

    print_section("HEDGE DELTA PORTFOLIO (via BTC-PERPETUAL)")
    hedge = compute_hedge_order(combined_greeks, current_hedge_qty, spot)
    display_hedge(hedge)

    # ── Rebalancement automatique si drift > seuil ────────────────────────────
    if hedge["needs_rebalance"]:
        old_qty    = current_hedge_qty
        old_avg    = float(hedge_data.get("avg_entry", spot))
        new_qty    = hedge["target_hedge_qty"]     # négatif (short)
        order_qty  = new_qty - old_qty             # delta à trader

        # Prix d'entrée moyen pondéré du hedge (VWAP des exécutions)
        # short qty négatif -> on prend les valeurs absolues pour le calcul
        abs_old  = abs(old_qty)
        abs_ord  = abs(order_qty)
        abs_new  = abs(new_qty)
        if abs_new < 1e-8:
            new_avg = spot                           # position fermée entièrement
        elif abs_new > abs_old:
            # On augmente le short -> VWAP des deux exécutions
            new_avg = (abs_old * old_avg + abs_ord * spot) / abs_new
        else:
            # On réduit le short (rachat partiel) -> prix entrée du reste inchangé
            new_avg = old_avg

        # PnL réalisé sur la portion clôturée (buy-back ou short supplémentaire)
        # Formule : short vendu à old_avg, racheté/augmenté à spot
        # Pour un rachat (order_qty > 0) : pnl = -(order_qty) * (spot - old_avg)
        #   ex: racheté 0.061 BTC @ 63329, avg entrée 61598 → perte = -0.061*(63329-61598) = -$106
        # Pour un short supplémentaire (order_qty < 0) : aucun PnL réalisé, juste new avg
        if order_qty > 0 and abs_new < abs_old:
            # Rachat partiel → réalise le PnL sur la portion fermée
            realized_this = -order_qty * (spot - old_avg)
        elif abs_new < 1e-8:
            # Clôture totale → réalise le PnL sur toute la position
            realized_this = abs_old * (old_avg - spot)
        else:
            realized_this = 0.0  # short augmenté : pas de PnL réalisé

        prev_realized = float(hedge_data.get("realized_pnl_usd", 0.0))
        new_realized  = round(prev_realized + realized_this, 2)

        # Mettre à jour le hedge partagé dans state["hedge"]
        if "hedge" not in state:
            state["hedge"] = {}
        state["hedge"]["qty"]              = round(new_qty, 5)
        state["hedge"]["avg_entry"]        = round(new_avg, 2)
        state["hedge"]["rebalances"]       = hedge_data.get("rebalances", 0) + 1
        state["hedge"]["realized_pnl_usd"] = new_realized

        # Enregistrer le rebalancement dans l'historique du hedge
        _vwap_note = (
            "rachat partiel — VWAP entrée inchangé"   if order_qty > 0 and abs_new < abs_old
            else "short augmenté — VWAP recalculé"     if order_qty < 0 and abs_new > abs_old
            else "position fermée"                     if abs_new < 1e-8
            else ""
        )
        _delta_after_pct = round((hedge["delta_drift"] - order_qty) * 100, 3)
        rebal_entry = {
            "ts":        now_dt(),
            "side":      "SELL" if order_qty < 0 else "BUY",
            "qty":       round(order_qty, 5),
            "spot":      round(spot, 2),
            "qty_before": round(old_qty, 5),
            "qty_after":  round(new_qty, 5),
            "vwap_before": round(old_avg, 2),
            "vwap_after":  round(new_avg, 2),
            "drift":       round(hedge["delta_drift"], 5),
            "realized_pnl_usd": round(realized_this, 2),
            "realized_cumul_usd": new_realized,
            "delta_net_after_pct": _delta_after_pct,
            "note":        _vwap_note,
        }
        if "history" not in state["hedge"]:
            state["hedge"]["history"] = []
        state["hedge"]["history"].append(rebal_entry)

        print(f"\n  [AUTO-REBALANCE]")
        print(f"  Ordre      : {'SELL' if order_qty < 0 else 'BUY'} "
              f"{abs(order_qty):.5f} BTC-PERPETUAL @ ~${spot:,.2f}")
        print(f"  Hedge qty  : {old_qty:+.5f} -> {new_qty:+.5f} BTC")
        print(f"  Avg entry  : ${old_avg:,.2f} -> ${new_avg:,.2f} (VWAP)")

    # ── PnL ───────────────────────────────────────────────────────────────────
    print_section("PnL MARK-TO-MARKET")
    pos = state.get("open") or (open_positions[0] if open_positions else {})
    pnl = compute_pnl(pos, spot)
    display_pnl(pnl)

    # ── Alertes risque ────────────────────────────────────────────────────────
    print_section("ALERTES RISQUE")
    alerts = []
    if abs(pos_greeks["pos_gamma"]) > 0.0005 * CONTRACTS:
        alerts.append(f"  [!] Gamma eleve: {pos_greeks['pos_gamma']:.6f}  "
                      f"-> sensibilite au mouvement accrue")
    if pos_greeks["tte_days"] < ROLL_TRIGGER:
        gamma_pts_alert = abs(pos_greeks.get("pos_gamma", 7e-5)) * spot * 0.01 * 100
        if gamma_pts_alert > GAMMA_ROLL_THRESHOLD:
            alerts.append(f"  [!!] ROLL IMMINENT: TTE {pos_greeks['tte_days']:.2f}j "
                          f"ET gamma {gamma_pts_alert:.2f}pts > seuil {GAMMA_ROLL_THRESHOLD}pts — ATM danger")
        else:
            alerts.append(f"  [~] TTE {pos_greeks['tte_days']:.2f}j dans fenetre roll "
                          f"mais gamma {gamma_pts_alert:.2f}pts <= seuil — OTM OK, HOLD")
    if abs(pos_greeks["pos_vega"]) > 0.05 * CONTRACTS:
        alerts.append(f"  [!] Vega eleve: {pos_greeks['pos_vega']:.4f}  "
                      f"-> exposition IV significative")
    if abs(pnl["pnl_pct"]) > 50:
        alerts.append(f"  [!!] PnL > 50% en mouvement: {pnl['pnl_pct']:+.1f}% "
                      f"-> verifier stop-loss")
    # Hedge drift avec seuil dynamique
    thr_btc = hedge.get("hedge_threshold_btc", 0.03)
    thr_pct = hedge.get("hedge_threshold_pct", 3.0)
    if hedge["needs_rebalance"]:
        alerts.append(f"  [!!] REBALANCER: drift {abs(hedge['delta_drift'])*100:.2f}% "
                      f"> seuil IV-adj {thr_pct:.1f}%")

    if alerts:
        for a in alerts:
            print(a)
    else:
        print("  Aucune alerte. Position dans les parametres.")

    # ── Résumé historique ─────────────────────────────────────────────────────
    if state["history"]:
        print_section("HISTORIQUE DES ROLLS")
        hist_df = pd.DataFrame(state["history"])
        total_pnl = hist_df["pnl_usd"].sum()
        print(f"  {len(hist_df)} rolls effectues  |  PnL cumule: "
              f"{'+' if total_pnl>=0 else ''}{total_pnl:.2f} USD")
        print()
        for _, row in hist_df.tail(5).iterrows():
            print(f"  {row['instrument_name']:<32}  "
                  f"entry={row['entry_price']:.5f}  "
                  f"exit={row.get('exit_price', 0):.5f}  "
                  f"pnl={'+' if row['pnl_usd']>=0 else ''}{row['pnl_usd']:.2f}$")

    # ── Sauvegarder + sync Gist ───────────────────────────────────────────────
    # Supprimer la clé compat "open" avant de sauvegarder (évite duplication)
    state.pop("open", None)
    save_positions(state)
    push_positions()   # sync automatique vers GitHub Gist

    # ── Export CSV snapshot ───────────────────────────────────────────────────
    tag = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    hedge_save = state.get("hedge", {})
    snap = {**combined_greeks, **hedge,
            "hedge_qty": hedge_save.get("qty", 0),
            "pnl_usd": pnl["pnl_usd"],
            "total_pnl_usd": pnl["total_pnl_usd"],
            "timestamp": now_dt()}
    pd.DataFrame([snap]).to_csv(
        OUTPUT_DIR / f"{currency}_{tag}_hedge_snapshot.csv", index=False)

    print_separator()
    print(f"  Snapshot sauvegarde dans output/")
    print_separator()


def monitor_loop(interval_minutes: int = 30, currency: str = CURRENCY):
    """Boucle de monitoring: execute run_once toutes les N minutes."""
    print(f"Mode MONITOR — refresh toutes les {interval_minutes} min")
    print("Ctrl+C pour arreter\n")
    while True:
        try:
            run_once(currency)
        except Exception as e:
            print(f"\n[ERREUR] {e}")
        print(f"\nProchain refresh dans {interval_minutes} min...")
        time.sleep(interval_minutes * 60)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Greeks & Hedge Engine")
    parser.add_argument("--run",      action="store_true", help="Scan unique")
    parser.add_argument("--monitor",  action="store_true", help="Boucle de monitoring")
    parser.add_argument("--interval", type=int, default=30,
                        help="Intervalle monitor en minutes (default: 30)")
    parser.add_argument("--currency", type=str, default=CURRENCY,
                        help="BTC ou ETH (default: BTC)")
    parser.add_argument("--reset",    action="store_true",
                        help="Remet a zero les positions sauvegardees")
    args = parser.parse_args()

    if args.reset:
        POSITIONS_FILE.unlink(missing_ok=True)
        print("Positions remises a zero.")
    elif args.monitor:
        monitor_loop(args.interval, args.currency)
    else:
        run_once(args.currency)
