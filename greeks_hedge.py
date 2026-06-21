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

# ── Gestion de portefeuille ────────────────────────────────────────────────────
MAX_PORTFOLIO_BTC        = 5.0   # notionnel total max en BTC (somme des contracts)
BA_MAX_PCT               = 50.0  # spread bid/ask max — garde-fou anti-illiquidité totale seulement
                                 # (le filtre anti-poussière est MIN_PREMIUM_USD, pas le %)
MIN_PREMIUM_USD          = 150.0 # prime min encaissée au bid (par BTC) — écarte la poussière far-OTM
                                 # (positions à ~38$ encaissés) : backtest 4 ans → Calmar 3.56→4.40, DD −17%.
                                 # NB : plancher en $ ABSOLU, calibré BTC. Pour ETH (sous-jacent ~20× moins
                                 # cher) il faudrait un plancher relatif (% du spot), sinon il bloque tout.
ENTRY_SCORE_MIN          = 0.45  # score minimum pour entrée — abaissé avec SKEW_NORM=0.60/IVHV_NORM=1.50
                                 # (ces normes dé-saturent donc baissent l'échelle des scores → le seuil suit)
ALWAYS_IN_POSITION       = False # C2 : ne PAS forcer l'entrée sur book vide si aucun candidat ne passe le seuil
                                 # (rester à plat les jours faibles → MaxDD −28% à PnL neutre, voir README)
# Poids du score composite (C1 : repondéré vers le skew, backtest 4 ans → MaxDD −20% à PnL neutre).
# Le yield est le composant le plus risqué (chasse les strikes proches) → réduit ; le skew (options
# loin OTM, prime de crash riche, moins gap-sensibles) → relevé.
# NB : l'expérience skew=0.65 / SKEW_NORM=0.60 a été annulée — sous le vrai skew pentu elle
# corner-solutionnait sur le put le plus loin OTM passant le plancher de prime (delta −0.05,
# yield ~6%, prime au plancher), au lieu des trades équilibrés (delta −0.10/−0.15). Retour au
# couple validé. À re-trancher proprement avec plusieurs semaines de vraies surfaces.
SCORE_W_IVHV             = 0.30  # poids VRP (IV au bid / HV blend)
SCORE_W_YIELD            = 0.25  # poids yield ajusté au risque (réduit — le plus gap-dangereux)
SCORE_W_SKEW             = 0.45  # poids skew vs ATM (relevé, mais pas dominant → évite le coin deep-OTM)
SKEW_NORM                = 0.60  # normalisation s_skew = clamp(skew/SKEW_NORM) — entre-deux (dé-sature
                                 # partiellement vs 0.20 ; le vrai skew Deribit va jusqu'à ~80%)
IVHV_NORM                = 1.50  # normalisation s_iv_hv = clamp((bid_iv/HV−1)/IVHV_NORM, 0,1) — entre-deux

# Circuit breaker — palier dur (fermeture totale), calibré par backtest 2023-2026
CB_MOVE_3D_PCT           = 10.0  # ferme tout si move spot 3j < −10% (baisse seule — un pump est inoffensif pour des short puts)
CB_DVOL_3D_PTS           = 12.0  # ou si DVOL a pris +12 pts en 3j
CB_REENTRY_MOVE_PCT      = 4.0   # re-entrée (depuis fermeture) : |move 3j| < 4% ET HV5 < HV10
# Circuit breaker — palier d'allègement gradué (backtest 4 ans : DD −20% BTC / −50% ETH à PnL ~neutre)
GRADUATED_CB             = True  # active le palier intermédiaire avant la fermeture totale
CB_T1_MOVE_1D_PCT        = 5.0   # allège le book si chute spot >5% en 1 jour (crisis-alpha)
CB_T1_MOVE_3D_PCT        = 6.0   # ou >6% en 3 jours
CB_T1_KEEP               = 0.30  # fraction du book conservée à l'allègement (on rachète 70%)
CB_T1_RESTORE_MOVE_PCT   = 3.0   # reprise pleine taille quand |move 3j| < 3% (sans attendre HV5<HV10)
ENTRY_IV_HV_MIN          = 1.10  # ratio IV/HV minimum pour entrée opportuniste
ENTRY_SCORE_REENTRY_BOOST= 0.05  # amélioration score nécessaire pour re-entrer un instrument déjà tenu
DELTA_MIN_SPACING        = 0.08  # espacement min |delta| entre positions sur la même expiry
GAMMA_PENALTY_START      = 5.0   # gamma_pts en dessous duquel aucune pénalité
GAMMA_SCORE_CAP          = 10.0  # gamma_pts au-delà duquel le score est réduit à 0
                                  # pénalité linéaire entre GAMMA_PENALTY_START et GAMMA_SCORE_CAP
                                  # ex: gamma=5 → ×1.00 ; gamma=7.5 → ×0.50 ; gamma≥10 → éliminé
SCAN_TTE_MIN       = 1.0  # TTE min pour le scan (roll + opportuniste)
SCAN_TTE_MAX       = 30.0 # TTE max pour le scan
SCAN_DELTA_MIN     = -0.30  # plafond d'exposition : pas plus proche de l'ATM que -0.30
SCAN_DELTA_MAX     = 0.0    # pas de plancher : les puts loin OTM (petit delta) sont éligibles
# Sizing score-based : contracts = round(score, 1) BTC, max portfolio MAX_PORTFOLIO_BTC
# Diversification : 1 seule entrée opportuniste par run_once()
#   - espacement delta >= DELTA_MIN_SPACING entre positions de même expiry
#   - re-entrée sur instrument déjà tenu si score > entry_score + ENTRY_SCORE_REENTRY_BOOST


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
        "iv_at_entry":      instrument.get("bid_iv") or instrument["mark_iv"],
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


# ── Circuit breaker ───────────────────────────────────────────────────────────

def check_circuit_breaker(currency: str, spot: float, hv_10d: float) -> dict:
    """
    Calcule les métriques du circuit breaker :
      - move_3d_pct  : variation du spot vs il y a 72h
      - dvol_3d_chg  : variation du DVOL vs il y a 72h (pts)
      - hv_5d        : vol réalisée 5j annualisée
      - triggered    : |move 3j| > CB_MOVE_3D_PCT ou DVOL +CB_DVOL_3D_PTS
      - reentry_ok   : HV5 < HV10 (réalisé court se retourne) et |move 3j| < CB_REENTRY_MOVE_PCT
    """
    move_3d_pct = None
    move_1d_pct = None
    try:
        end_ts   = now_ms()
        start_ts = end_ts - 80 * 3600 * 1000
        cd = get("get_tradingview_chart_data", {
            "instrument_name": f"{currency}-PERPETUAL",
            "start_timestamp": start_ts, "end_timestamp": end_ts, "resolution": "60"})
        pairs = [(t, c) for t, c in zip(cd.get("ticks", []), cd.get("close", [])) if c]
        if pairs:
            for horizon_h, setter in ((72, "3d"), (24, "1d")):
                target = end_ts - horizon_h * 3600 * 1000
                ref = min(pairs, key=lambda r: abs(r[0] - target))
                if abs(ref[0] - target) < 6 * 3600 * 1000:
                    val = (spot / ref[1] - 1) * 100
                    if setter == "3d": move_3d_pct = val
                    else:              move_1d_pct = val
    except Exception:
        pass

    dvol_3d_chg = None
    try:
        end_ts   = now_ms()
        start_ts = end_ts - 80 * 3600 * 1000
        dv = get("get_volatility_index_data", {
            "currency": currency, "start_timestamp": start_ts,
            "end_timestamp": end_ts, "resolution": "3600"})
        rows = [(r[0], r[4]) for r in dv.get("data", []) if r[4]]
        if rows:
            curr = rows[-1][1]
            target = end_ts - 72 * 3600 * 1000
            ref = min(rows, key=lambda r: abs(r[0] - target))
            if abs(ref[0] - target) < 6 * 3600 * 1000:
                dvol_3d_chg = curr - ref[1]
    except Exception:
        pass

    hv_5d = None
    try:
        end_ts   = now_ms()
        start_ts = end_ts - 10 * 24 * 3600 * 1000
        cd = get("get_tradingview_chart_data", {
            "instrument_name": f"{currency}-PERPETUAL",
            "start_timestamp": start_ts, "end_timestamp": end_ts, "resolution": "1D"})
        closes = [c for c in cd.get("close", []) if c]
        if len(closes) >= 6:
            w = closes[-6:]
            rets = [math.log(w[j] / w[j-1]) for j in range(1, len(w))]
            hv_5d = math.sqrt(sum(r * r for r in rets) / len(rets)) * math.sqrt(365) * 100
    except Exception:
        pass

    # Baisse uniquement : un move haussier fait fondre les short puts (gamma décroissant à la hausse)
    triggered = ((move_3d_pct is not None and move_3d_pct < -CB_MOVE_3D_PCT)
                 or (dvol_3d_chg is not None and dvol_3d_chg > CB_DVOL_3D_PTS))
    reentry_ok = (hv_5d is not None and hv_5d < hv_10d
                  and move_3d_pct is not None and abs(move_3d_pct) < CB_REENTRY_MOVE_PCT)

    # Palier d'allègement gradué (chute sèche 1j OU baisse 3j) ; reprise quand le spot se stabilise
    tier1_triggered = GRADUATED_CB and (
        (move_1d_pct is not None and move_1d_pct < -CB_T1_MOVE_1D_PCT)
        or (move_3d_pct is not None and move_3d_pct < -CB_T1_MOVE_3D_PCT))
    tier1_restore = (move_3d_pct is not None and abs(move_3d_pct) < CB_T1_RESTORE_MOVE_PCT)

    return {
        "move_3d_pct": round(move_3d_pct, 2) if move_3d_pct is not None else None,
        "move_1d_pct": round(move_1d_pct, 2) if move_1d_pct is not None else None,
        "dvol_3d_chg": round(dvol_3d_chg, 2) if dvol_3d_chg is not None else None,
        "hv_5d":       round(hv_5d, 2) if hv_5d is not None else None,
        "triggered":   triggered,
        "reentry_ok":  reentry_ok,
        "tier1_triggered": tier1_triggered,
        "tier1_restore":   tier1_restore,
    }


def apply_circuit_breaker(state: dict, spot: float, cb: dict) -> bool:
    """
    Applique le circuit breaker à l'état :
      - déclenchement : rachat de toutes les positions (à l'ask), hedge à plat, risk_off=True
      - re-entrée     : risk_off=False quand les conditions sont réunies
    Retourne True si l'état a changé.
    """
    risk_off = bool(state.get("risk_off", False))

    if not risk_off and state.get("positions") and cb["triggered"]:
        print_section("CIRCUIT BREAKER DECLENCHE")
        print(f"  Move 3j : {cb['move_3d_pct']}%  (seuil {CB_MOVE_3D_PCT}%)")
        print(f"  DVOL 3j : {cb['dvol_3d_chg']:+} pts  (seuil +{CB_DVOL_3D_PTS} pts)" if cb["dvol_3d_chg"] is not None else "  DVOL 3j : n/a")
        # 1) Rachat de toutes les positions à l'ask (on paie le spread de sortie)
        for pos in state.get("positions", []):
            exit_p = pos["entry_price"]
            try:
                t = fetch_ticker_full(pos["instrument_name"])
                exit_p = t.get("best_ask_price") or t.get("mark_price") or exit_p
            except Exception:
                pass
            n = float(pos.get("contracts", 1))
            entry_spot = float(pos.get("entry_spot", spot))
            pnl_usd = round((pos["entry_price"] * entry_spot - exit_p * spot) * n, 2)
            closed = {**pos,
                      "exit_price":  exit_p,
                      "exit_spot":   spot,
                      "exit_ts":     now_dt(),
                      "exit_reason": "circuit_breaker",
                      "pnl_btc":     round((pos["entry_price"] - exit_p) * n, 6),
                      "pnl_usd":     pnl_usd}
            state.setdefault("history", []).append(closed)
            print(f"  [CB] Rachat {pos['instrument_name']}  exit {exit_p:.5f} BTC  PnL {pnl_usd:+.2f}$")
        state["positions"] = []
        state["open"] = None

        # 2) Hedge à plat
        hd = state.setdefault("hedge", {})
        qty = float(hd.get("qty", 0.0))
        if abs(qty) > 1e-8:
            avg = float(hd.get("avg_entry", spot))
            realized = abs(qty) * (avg - spot)   # short : gain si spot < avg
            hd["realized_pnl_usd"] = round(float(hd.get("realized_pnl_usd", 0.0)) + realized, 2)
            hd.setdefault("history", []).append({
                "ts":         now_dt(),
                "side":       "BUY",
                "qty":        round(abs(qty), 5),
                "spot":       round(spot, 2),
                "qty_before": round(qty, 5),
                "qty_after":  0.0,
                "vwap_before": round(avg, 2),
                "vwap_after":  0.0,
                "realized_pnl_usd": round(realized, 2),
                "note":       "circuit breaker — hedge a plat",
            })
            hd["qty"] = 0.0
            print(f"  [CB] Hedge a plat : rachat {abs(qty):.5f} BTC-PERP  PnL réalisé {realized:+.2f}$")

        state["risk_off"] = True
        state["cb_reduced"] = False   # la fermeture totale supplante l'allègement
        state["risk_off_info"] = {
            "ts":          now_dt(),
            "move_3d_pct": cb["move_3d_pct"],
            "dvol_3d_chg": cb["dvol_3d_chg"],
        }
        return True

    # ── Palier d'allègement gradué (tier 1) : on rachète une fraction, on garde le reste ──
    reduced = bool(state.get("cb_reduced", False))
    if GRADUATED_CB and not risk_off and not reduced and state.get("positions") and cb.get("tier1_triggered"):
        print_section("CIRCUIT BREAKER — ALLEGEMENT (palier 1)")
        print(f"  Move 1j : {cb.get('move_1d_pct')}%  (seuil −{CB_T1_MOVE_1D_PCT}%)  |  "
              f"Move 3j : {cb.get('move_3d_pct')}%  (seuil −{CB_T1_MOVE_3D_PCT}%)")
        print(f"  On conserve {CB_T1_KEEP*100:.0f}% du book, rachat du reste a l'ask.")
        for pos in state.get("positions", []):
            n = float(pos.get("contracts", 1))
            sell_n = round(n * (1.0 - CB_T1_KEEP), 6)
            keep_n = round(n - sell_n, 6)
            if sell_n <= 1e-9:
                continue
            exit_p = pos["entry_price"]
            try:
                t = fetch_ticker_full(pos["instrument_name"])
                exit_p = t.get("best_ask_price") or t.get("mark_price") or exit_p
            except Exception:
                pass
            entry_spot = float(pos.get("entry_spot", spot))
            pnl_usd = round((pos["entry_price"] * entry_spot - exit_p * spot) * sell_n, 2)
            state.setdefault("history", []).append({
                **pos, "contracts": sell_n,
                "exit_price": exit_p, "exit_spot": spot, "exit_ts": now_dt(),
                "exit_reason": "cb_tier1_trim",
                "pnl_btc": round((pos["entry_price"] - exit_p) * sell_n, 6),
                "pnl_usd": pnl_usd})
            pos["contracts"] = keep_n
            print(f"  [CB-T1] Allege {pos['instrument_name']} : -{sell_n:.2f} (reste {keep_n:.2f})  PnL {pnl_usd:+.2f}$")
        state["cb_reduced"] = True
        state["cb_reduced_info"] = {"ts": now_dt(),
                                    "move_1d_pct": cb.get("move_1d_pct"),
                                    "move_3d_pct": cb.get("move_3d_pct")}
        # NB : le hedge suit automatiquement au rebalance unifié de fin de run (book réduit)
        return True

    if reduced and cb.get("tier1_restore"):
        print_section("CIRCUIT BREAKER — REPRISE PLEINE TAILLE (palier 1)")
        print(f"  move 3j {cb.get('move_3d_pct')}% < {CB_T1_RESTORE_MOVE_PCT}% -> cap d'allegement relache")
        state["cb_reduced"] = False
        return True

    if risk_off and cb["reentry_ok"]:
        print_section("CIRCUIT BREAKER — RE-ENTREE")
        print(f"  HV5 {cb['hv_5d']}% < HV10  |  move 3j {cb['move_3d_pct']}% < {CB_REENTRY_MOVE_PCT}%")
        state["risk_off"] = False
        info = state.setdefault("risk_off_info", {})
        info["reentry_ts"] = now_dt()
        return True

    if risk_off:
        print_section("CIRCUIT BREAKER — RISK-OFF MAINTENU")
        print(f"  HV5 {cb['hv_5d']}% (doit < HV10)  |  move 3j {cb['move_3d_pct']}% (doit < {CB_REENTRY_MOVE_PCT}%)")
    return False


# ── Main run ──────────────────────────────────────────────────────────────────

def expire_positions(state: dict, spot: float) -> list[str]:
    """
    Déplace les positions expirées de positions[] vers history[].
    Retourne la liste des instruments expirés (pour affichage).
    """
    now = datetime.now(timezone.utc)
    expired_names = []
    remaining = []

    for pos in state.get("positions", []):
        try:
            expiry = datetime.fromisoformat(pos["expiry_dt"])
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except Exception:
            remaining.append(pos)
            continue

        if now < expiry:
            remaining.append(pos)
            continue

        # Position expirée : calculer le PnL final
        exit_price = 0.0  # OTM → expire sans valeur par défaut
        try:
            t = fetch_ticker_full(pos["instrument_name"])
            exit_price = t.get("mark_price") or 0.0
        except Exception:
            pass  # instrument retiré de l'API → on suppose worthless

        n = pos.get("contracts", 1)
        pnl_btc = (pos["entry_price"] - exit_price) * n
        pnl_usd = round(pnl_btc * spot, 2)

        closed = {
            **pos,
            "exit_price":    exit_price,
            "exit_spot":     spot,
            "exit_ts":       now_dt(),
            "tte_at_exit":   0.0,
            "exit_reason":   "expiration",
            "pnl_btc":       round(pnl_btc, 6),
            "pnl_usd":       pnl_usd,
        }
        state.setdefault("history", []).append(closed)
        expired_names.append(pos["instrument_name"])
        print(f"  [EXPIRATION] {pos['instrument_name']} clôturée "
              f"— exit {exit_price:.5f} BTC  PnL {'+' if pnl_usd>=0 else ''}{pnl_usd:.2f} USD")

    state["positions"] = remaining
    return expired_names


def run_once(currency: str = CURRENCY, verbose: bool = True):
    global pos_greeks_cache

    print_separator()
    print(f"  Greeks & Hedge Engine — {now_dt()}")
    print(f"  Currency: {currency}  |  Contracts: {CONTRACTS}")
    print_separator()

    spot = fetch_spot(currency)
    print(f"\n  Spot {currency}: ${spot:,.2f}")

    state = load_positions()
    _entries_this_run: list = []   # instruments ouverts ce run (hedge délégué au rebalance unifié)

    # ── Expiration automatique ─────────────────────────────────────────────────
    expired = expire_positions(state, spot)
    if expired:
        # Recalculer l'état "open" après expiration
        state["open"] = state["positions"][0] if state.get("positions") else None

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

    # ── Marché : contexte de vol (une seule fois) ─────────────────────────────
    ctx = get_market_context(currency)

    # ── Circuit breaker ────────────────────────────────────────────────────────
    cb = check_circuit_breaker(currency, spot, ctx["hv_10d"])
    print_section("CIRCUIT BREAKER CHECK")
    print(f"  Move 3j : {cb['move_3d_pct']}%  (déclenche si < -{CB_MOVE_3D_PCT}%)")
    print(f"  DVOL 3j : {cb['dvol_3d_chg']:+} pts  (déclenche si > +{CB_DVOL_3D_PTS} pts)"
          if cb["dvol_3d_chg"] is not None else "  DVOL 3j : n/a")
    print(f"  HV5/HV10: {cb['hv_5d']}% / {ctx['hv_10d']}%  |  Etat : {'RISK-OFF' if state.get('risk_off') else 'normal'}")
    if apply_circuit_breaker(state, spot, cb):
        save_positions(state)
    risk_off = bool(state.get("risk_off", False))

    # ── Roll ou ouverture si portfolio vide ────────────────────────────────────
    open_positions_now = state.get("positions", [])
    must_open = ALWAYS_IN_POSITION and len(open_positions_now) == 0 and not risk_off   # garantie "toujours ≥1" si activée

    if (state["open"] is None or must_open) and not risk_off:
        reason = "portfolio vide -- ouverture obligatoire" if must_open else "roll declenche"
        print_section(f"SELECTION CANDIDAT ({reason.upper()})")
        print(f"  HV 10j: {ctx['hv_10d']:.1f}%  |  IV: {ctx['curr_iv']:.1f}%  "
              f"|  IV/HV: {ctx['iv_hv_ratio']:.2f}x  |  Regime: {ctx['regime']}")

        candidates = fetch_scored_candidates(
            currency, spot,
            ctx["hv_blend"], ctx["iv_min"], ctx["iv_max"], ctx["curr_iv"],
        )
        if candidates.empty:
            print("  Aucun candidat liquide trouve (filtre B/A). On reessaie plus tard.")
            if not must_open:
                save_positions(state)
                return
            # Si portfolio vide : fallback sans filtre B/A (on entre quand meme)
            candidates = fetch_scored_candidates(
                currency, spot, ctx["hv_blend"], ctx["iv_min"], ctx["iv_max"], ctx["curr_iv"],
                ba_max_pct=999
            )
            if candidates.empty:
                print("  Aucune option trouvee du tout. Abandon.")
                save_positions(state)
                return

        best = candidates.iloc[0]

        # C2 : entrée non forcée → exiger le seuil de score + signal. Sinon, rester à plat.
        if not must_open and (float(best["score"]) < ENTRY_SCORE_MIN or not ctx["signal_ok"]):
            print(f"  [Pas d'entree] meilleur score {best['score']:.3f} < seuil {ENTRY_SCORE_MIN:.2f} "
                  f"ou signal KO -- book laisse a plat")
            save_positions(state)
            return

        used_btc = sum(float(p.get("contracts", 1)) for p in state.get("positions", []))
        sizing = compute_sizing(float(best["score"]), used_btc, ctx["iv_rank"])
        _eff_cap = MAX_PORTFOLIO_BTC * (CB_T1_KEEP if state.get("cb_reduced") else 1.0)
        sizing = min(sizing, max(0.0, round(_eff_cap - used_btc, 1)))   # cap réduit si allègement
        if sizing < 0.1:
            print("  [Pas d'entree] cap d'allegement (CB tier 1) atteint -- pas d'ouverture")
            save_positions(state)
            return
        new_pos = open_position_from_candidate(best, spot, contracts=sizing)

        # NB : le hedge n'est PAS exécuté ici. La position est ajoutée, puis l'unique
        # rebalance de fin de run calcule la cible delta nette de TOUT le portefeuille
        # et exécute un seul ordre. Hedger inline ici déclenchait un aller-retour
        # (SELL du delta du lot, puis BUY du rebalance qui re-snap au net exact).
        state.setdefault("positions", []).append(new_pos)
        state["open"] = new_pos
        _entries_this_run.append(new_pos["instrument_name"])

        print(f"  [OUVERTURE] {new_pos['instrument_name']}")
        print(f"    Score    : {best['score']:.3f}  (IV/HV {best['iv_hv_ratio']:.2f}x  rank {best['s_rank']*100:.0f}%  yield {best['yield_ann_pct']:.1f}%/an)")
        print(f"    Strike   : {best['strike']:,.0f}  ({best['moneyness']:+.1f}%)")
        print(f"    TTE      : {best['tte_days']:.1f}j  |  Delta {best['delta']:+.3f}  |  IV {best['mark_iv']:.1f}%")
        print(f"    Prix     : {new_pos['entry_price']:.5f} BTC = ${new_pos['entry_price_usd']:,.0f}  (bid)")
        print(f"    B/A      : {best['ba_pct']:.1f}%  |  OI {best['open_interest']:.0f}")
        print(f"    Hedge    : delta du portefeuille recalculé au rebalance unifié (ci-dessous)")

    # ── Entrée opportuniste (positions < MAX) ──────────────────────────────────
    open_positions_now = state.get("positions", [])
    used_btc_now = sum(float(p.get("contracts", 1)) for p in open_positions_now)

    # Infos des positions tenues pour filtres de diversification et re-entrée
    held_info = {
        p["instrument_name"]: {
            "expiry": str(p.get("expiry_dt", ""))[:10],
            "delta":  float(p.get("delta_at_entry", 0)),
            "score":  float(p.get("entry_score", ENTRY_SCORE_MIN)),
        }
        for p in open_positions_now
    }

    def _candidate_allowed(row) -> bool:
        """Renvoie True si le candidat peut être entré (diversification + re-entrée)."""
        name    = row["instrument_name"]
        c_exp   = str(row.get("expiry_dt", ""))[:10]
        c_delta = float(row.get("delta", 0))
        c_score = float(row.get("score", 0))

        # Re-entrée sur instrument déjà tenu : seulement si score nettement meilleur
        if name in held_info:
            return c_score > held_info[name]["score"] + ENTRY_SCORE_REENTRY_BOOST

        # Diversification : candidat proche en delta sur même expiry → traité comme ré-entrée implicite
        # (autorisé seulement si score nettement meilleur que la position similaire tenue)
        for h in held_info.values():
            if h["expiry"] == c_exp and abs(c_delta - h["delta"]) < DELTA_MIN_SPACING:
                return c_score > h["score"] + ENTRY_SCORE_REENTRY_BOOST
        return True

    # Cap effectif réduit pendant l'allègement gradué (CB tier 1)
    eff_cap = MAX_PORTFOLIO_BTC * (CB_T1_KEEP if state.get("cb_reduced") else 1.0)
    if used_btc_now < eff_cap and ctx["signal_ok"] and not risk_off:
        candidates = fetch_scored_candidates(
            currency, spot, ctx["hv_blend"], ctx["iv_min"], ctx["iv_max"], ctx["curr_iv"],
        )
        # Appliquer les filtres (diversification + re-entrée)
        candidates = candidates[candidates.apply(_candidate_allowed, axis=1)]
        if not candidates.empty and candidates.iloc[0]["score"] >= ENTRY_SCORE_MIN:
            best2 = candidates.iloc[0]
            used_btc2 = sum(float(p.get("contracts", 1)) for p in state.get("positions", []))
            sizing2 = compute_sizing(float(best2["score"]), used_btc2, ctx["iv_rank"])
            sizing2 = min(sizing2, max(0.0, round(eff_cap - used_btc2, 1)))   # respecte le cap réduit
            if sizing2 < 0.1:
                save_positions(state); return
            new_pos2 = open_position_from_candidate(best2, spot, contracts=sizing2)

            # Hedge délégué au rebalance unifié de fin de run (évite l'aller-retour)
            state["positions"].append(new_pos2)
            _entries_this_run.append(new_pos2["instrument_name"])
            print_section("ENTREE OPPORTUNISTE")
            print(f"  [OUVERTURE] {new_pos2['instrument_name']}")
            print(f"    Score    : {best2['score']:.3f}  (IV/HV {best2['iv_hv_ratio']:.2f}x)")
            print(f"    Strike   : {best2['strike']:,.0f}  ({best2['moneyness']:+.1f}%)")
            print(f"    TTE      : {best2['tte_days']:.1f}j  |  Delta {best2['delta']:+.3f}  |  IV {best2['mark_iv']:.1f}%")
            print(f"    Prix     : {new_pos2['entry_price']:.5f} BTC = ${new_pos2['entry_price_usd']:,.0f}")
            print(f"    Hedge    : delta du portefeuille recalculé au rebalance unifié (ci-dessous)")
        else:
            score_top = candidates.iloc[0]["score"] if not candidates.empty else 0
            print(f"  [Opportuniste] score {score_top:.3f} < seuil {ENTRY_SCORE_MIN:.2f} ou IV/HV insuffisant -- pas d'entree")

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
        "tte_days":  min(g["tte_days"] for g in all_greeks),  # position la plus proche de l'expiry
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
        if _entries_this_run:
            _entry_lbl = ", ".join(_entries_this_run)
            _vwap_note = (f"hedge net après entrée ({_entry_lbl})"
                          + (f" · {_vwap_note}" if _vwap_note else ""))
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

    # ── Sauvegarder scan_entry.json (top opportunités pour le dashboard) ─────────
    try:
        _positions_now = state.get("positions", [])
        _held_info = {
            p["instrument_name"]: {
                "expiry": str(p.get("expiry_dt", ""))[:10],
                "delta":  float(p.get("delta_at_entry", 0)),
                "score":  float(p.get("entry_score", ENTRY_SCORE_MIN)),
            }
            for p in _positions_now
        }
        _scan_candidates = fetch_scored_candidates(
            currency, spot, ctx["hv_blend"], ctx["iv_min"], ctx["iv_max"], ctx["curr_iv"],
        )

        def _scan_row_status(row):
            """Retourne le statut du candidat pour le dashboard."""
            name    = row["instrument_name"]
            c_exp   = str(row.get("expiry_dt", ""))[:10]
            c_delta = float(row.get("delta", 0))
            c_score = float(row.get("score", 0))
            if name in _held_info:
                held_score = _held_info[name]["score"]
                reentry_ok = c_score > held_score + ENTRY_SCORE_REENTRY_BOOST
                return "held_reentry" if reentry_ok else "held"
            for h in _held_info.values():
                if h["expiry"] == c_exp and abs(c_delta - h["delta"]) < DELTA_MIN_SPACING:
                    reentry_ok = c_score > h["score"] + ENTRY_SCORE_REENTRY_BOOST
                    return "held_reentry" if reentry_ok else "filtered"
            return "eligible"

        if not _scan_candidates.empty:
            _scan_candidates = _scan_candidates.copy()
            _scan_candidates["status"] = _scan_candidates.apply(_scan_row_status, axis=1)
            # Pour le score de re-entrée, ajouter le score d'entrée initial si tenu
            _scan_candidates["held_entry_score"] = _scan_candidates["instrument_name"].map(
                lambda n: _held_info[n]["score"] if n in _held_info else None
            )

        # Top 7 par score, avec garantie d'au moins 1 candidat éligible visible
        _top7 = _scan_candidates.head(7).copy() if not _scan_candidates.empty else pd.DataFrame()
        if not _top7.empty and "eligible" not in _top7["status"].values:
            _eligible_extra = _scan_candidates[_scan_candidates["status"] == "eligible"]
            if not _eligible_extra.empty:
                _top7 = pd.concat([_top7, _eligible_extra.head(1)]).reset_index(drop=True)
        _scan_out = {
            "ts": now_dt(),
            "market_context": {
                "spot": round(spot, 2),
                "hv_10d": round(ctx["hv_10d"], 2),
                "hv_30d": round(ctx["hv_30d"], 2),
                "hv_blend": round(ctx["hv_blend"], 2),
                "hv_1d_chg": ctx.get("hv_1d_chg"),
                "iv_rank": round(ctx["iv_rank"], 3),
                "iv_hv_ratio": round(ctx["iv_hv_ratio"], 3),
                "curr_iv": round(ctx["curr_iv"], 2),
                "dvol_1d_chg": ctx.get("dvol_1d_chg"),
                "regime": ctx["regime"],
                "signal_ok": ctx["signal_ok"],
                "risk_off": risk_off,
                "cb_reduced": bool(state.get("cb_reduced", False)),
                "cb_move_3d": cb.get("move_3d_pct"),
                "cb_move_1d": cb.get("move_1d_pct"),
                "cb_dvol_3d": cb.get("dvol_3d_chg"),
                "cb_hv_5d": cb.get("hv_5d"),
            },
            "top7": _top7.to_dict(orient="records") if not _top7.empty else [],
        }
        (Path(__file__).parent / "scan_entry.json").write_text(
            json.dumps(_scan_out, indent=2, default=str)
        )
    except Exception as _e:
        print(f"  [warn] scan_entry.json non sauvegardé: {_e}")

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


# ── Entry Scanner & Portfolio Manager ────────────────────────────────────────

def fetch_hv(currency: str = CURRENCY, days: int = 10) -> float:
    """Volatilité historique réalisée sur N jours (log-returns daily closes)."""
    end_ts   = now_ms()
    start_ts = end_ts - (days + 5) * 24 * 3600 * 1000
    try:
        data   = get("get_tradingview_chart_data", {
            "instrument_name": f"{currency}-PERPETUAL",
            "start_timestamp": start_ts,
            "end_timestamp":   end_ts,
            "resolution":      "1D",
        })
        closes = data.get("close", [])
        closes = [c for c in closes if c]
        if len(closes) < 3:
            return 70.0
        closes = closes[-(days + 1):]
        log_rets = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
        mean  = sum(log_rets) / len(log_rets)
        var   = sum((r - mean) ** 2 for r in log_rets) / max(len(log_rets) - 1, 1)
        return round(math.sqrt(var) * math.sqrt(365) * 100, 2)
    except Exception:
        return 70.0


def fetch_iv_range(currency: str = CURRENCY, days: int = 30) -> tuple[float, float]:
    """(IV_min, IV_max) sur N jours via l'index DVOL de Deribit."""
    end_ts   = now_ms()
    start_ts = end_ts - days * 24 * 3600 * 1000
    try:
        data = get("get_volatility_index_data", {
            "currency":        currency,
            "start_timestamp": start_ts,
            "end_timestamp":   end_ts,
            "resolution":      "1D",
        })
        closes = [row[4] for row in data.get("data", []) if row[4]]
        if len(closes) >= 5:
            return min(closes), max(closes)
    except Exception:
        pass
    return 30.0, 120.0  # fallback


def fetch_scored_candidates(currency: str, spot: float,
                            hv_ref: float, iv_min: float, iv_max: float,
                            curr_iv: float = 50.0,
                            tte_min: float = SCAN_TTE_MIN,
                            tte_max: float = SCAN_TTE_MAX,
                            delta_min: float = SCAN_DELTA_MIN,
                            delta_max: float = SCAN_DELTA_MAX,
                            ba_max_pct: float = BA_MAX_PCT,
                            min_premium_usd: float = MIN_PREMIUM_USD) -> pd.DataFrame:
    """
    Scanne les puts OTM, calcule le score composite, filtre le spread B/A.
    hv_ref = HV blend (0.5×10j + 0.5×30j) servant de référence vol réalisée.
    min_premium_usd : prime min encaissée au bid (par BTC) — exclut les options trop
    bon marché (deep OTM à quelques $) indépendamment du spread.
    Retourne un DataFrame trié par score décroissant.
    """
    instruments = get("get_instruments", {"currency": currency, "kind": "option", "expired": "false"})
    now_t = now_ms()
    rows  = []

    for inst in instruments:
        if not inst["instrument_name"].endswith("-P"):
            continue
        tte = (inst["expiration_timestamp"] - now_t) / 86_400_000
        if not (tte_min <= tte <= tte_max):
            continue
        try:
            t      = fetch_ticker_full(inst["instrument_name"])
            greeks = t.get("greeks") or {}
            delta  = greeks.get("delta")
            mark_iv = t.get("mark_iv") or 0.0
            bid_iv  = t.get("bid_iv") or mark_iv   # IV implicite au bid (prix vendeur); fallback mid
            iv     = mark_iv                        # gardé pour affichage
            mark   = t.get("mark_price") or 0
            bid    = t.get("best_bid_price") or 0
            ask    = t.get("best_ask_price") or 0
            oi     = t.get("open_interest") or 0
            if delta is None or iv is None or mark == 0:
                continue
            if not (delta_min <= delta <= delta_max):
                continue

            # Filtre spread B/A (garde-fou anti-illiquidité)
            ba_pct = (ask - bid) / mark * 100 if mark > 0 else 999
            if ba_pct > ba_max_pct:
                continue

            # Filtre prime min : on encaisse le bid → exclut les options trop bon marché
            # (deep OTM à quelques $) qui ne valent pas la marge ni le risque de queue
            premium_bid_usd = bid * spot
            if premium_bid_usd < min_premium_usd:
                continue

            rows.append({
                "instrument_name": inst["instrument_name"],
                "strike":          inst["strike"],
                "expiry_ts":       inst["expiration_timestamp"],
                "expiry_dt":       pd.to_datetime(inst["expiration_timestamp"], unit="ms", utc=True),
                "tte_days":        round(tte, 2),
                "delta":           delta,
                "gamma":           greeks.get("gamma", 0),
                "vega":            greeks.get("vega", 0),
                "theta":           greeks.get("theta", 0),
                "mark_iv":         mark_iv,
                "bid_iv":          bid_iv,
                "mark_price":      mark,
                "bid_price":       bid,
                "ask_price":       ask,
                "open_interest":   oi,
                "moneyness":       round((inst["strike"] / spot - 1) * 100, 1),
                "ba_pct":          round(ba_pct, 1),
            })
        except Exception:
            continue

    if not rows:
        return pd.DataFrame()

    # ── IV ATM par échéance (référence skew) ──────────────────────────────────
    # Pour chaque expiry candidate, fetch l'IV du put dont le strike est le plus
    # proche du spot → mesure la richesse du strike vendu vs le centre du smile.
    atm_iv_by_exp: dict = {}
    cand_expiries = {r["expiry_ts"] for r in rows}
    for exp_ts in cand_expiries:
        puts_exp = [i for i in instruments
                    if i["instrument_name"].endswith("-P") and i["expiration_timestamp"] == exp_ts]
        if not puts_exp:
            continue
        atm_inst = min(puts_exp, key=lambda i: abs(i["strike"] - spot))
        try:
            atm_t = fetch_ticker_full(atm_inst["instrument_name"])
            atm_iv_by_exp[exp_ts] = atm_t.get("mark_iv") or curr_iv
        except Exception:
            atm_iv_by_exp[exp_ts] = curr_iv

    # ── Scoring ────────────────────────────────────────────────────────────────
    # rang DVOL 30j : sorti du score (commun à tous les candidats), utilisé par
    # compute_sizing comme multiplicateur d'agressivité. Conservé en colonne.
    s_rank = max(0.0, min(1.0, (curr_iv - iv_min) / max(iv_max - iv_min, 5)))
    for r in rows:
        tte_yr  = r["tte_days"] / 365
        bid_iv  = r["bid_iv"]
        bid     = r["bid_price"]
        mark    = r["mark_price"]

        # 1) VRP : IV au bid vs vol réalisée (HV blend 10j/30j)
        s_iv_hv = max(0.0, min(1.0, (bid_iv / hv_ref - 1.0) / IVHV_NORM))

        # 2) Skew : richesse du strike vs ATM de la même échéance
        atm_iv  = atm_iv_by_exp.get(r["expiry_ts"], curr_iv) or curr_iv
        skew    = bid_iv / atm_iv - 1.0
        s_skew  = max(0.0, min(1.0, skew / SKEW_NORM))   # 1.0 quand le put paie SKEW_NORM de plus que l'ATM

        # 3) Yield ajusté au risque : yield annualisé × distance au strike en vols réalisées
        #    z = OTM% / (HV × √TTE) — un yield élevé proche du strike vaut moins
        #    qu'un yield moyen loin du strike
        yield_a = bid / tte_yr if bid > 0 else mark / tte_yr
        otm_frac = abs(r["moneyness"]) / 100
        z_score  = otm_frac / max(hv_ref / 100 * math.sqrt(tte_yr), 1e-9)
        s_yield  = min(1.0, (yield_a * z_score) / 0.30)

        score_raw = SCORE_W_IVHV * s_iv_hv + SCORE_W_YIELD * s_yield + SCORE_W_SKEW * s_skew
        # Pénalité gamma : linéaire entre GAMMA_PENALTY_START (×1.0) et GAMMA_SCORE_CAP (×0.0)
        gamma_pts_val = r["gamma"] * spot * 0.01 * 100
        gamma_excess  = max(0.0, gamma_pts_val - GAMMA_PENALTY_START)
        gamma_factor  = max(0.0, 1.0 - gamma_excess / (GAMMA_SCORE_CAP - GAMMA_PENALTY_START))

        r.update({
            "premium_usd":   round(bid * spot if bid > 0 else mark * spot, 2),
            "gamma_pts":     round(gamma_pts_val, 2),
            "gamma_factor":  round(gamma_factor, 3),
            "yield_ann_pct": round(yield_a * 100, 1),
            "atm_iv":        round(atm_iv, 1),
            "skew_pct":      round(skew * 100, 1),
            "z_score":       round(z_score, 2),
            "score":         round(score_raw * gamma_factor, 3),
            "score_raw":     round(score_raw, 3),
            "s_iv_hv":       round(s_iv_hv, 3),
            "s_skew":        round(s_skew, 3),
            "s_rank":        round(s_rank, 3),
            "s_yield":       round(s_yield, 3),
            "iv_hv_ratio":   round(bid_iv / hv_ref, 3),
        })

    return pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)


def get_market_context(currency: str = CURRENCY) -> dict:
    """Retourne HV 10j/30j/blend, IV range 30j, IV courante et régime de vol."""
    hv_10d         = fetch_hv(currency, days=10)
    hv_30d         = fetch_hv(currency, days=30)
    # Blend 50/50 : garde la réactivité du 10j en amortissant l'effet falaise
    # (un seul gros jour qui entre/sort de la fenêtre 10j faisait basculer tous les scores)
    hv_blend       = round(0.5 * hv_10d + 0.5 * hv_30d, 2)
    iv_min, iv_max = fetch_iv_range(currency, days=30)

    # IV courante + move 1j : DVOL index live sur 26h pour avoir curr et prev_1d
    curr_iv     = 60.0
    dvol_1d_chg = None
    try:
        end_ts   = now_ms()
        start_ts = end_ts - 26 * 3600 * 1000   # 26h → couvre curr + point d'il y a ~24h
        # Bougies horaires (3600s) : 26 points sur 26h. En résolution 60s, l'API plafonne
        # le nombre de points (~1000) et ne renvoie que ~16h → le point à 24h manquait → None.
        dvol_data = get("get_volatility_index_data", {
            "currency":        currency,
            "start_timestamp": start_ts,
            "end_timestamp":   end_ts,
            "resolution":      "3600",
        })
        dvol_rows = [(r[0], r[4]) for r in dvol_data.get("data", []) if r[4]]
        if dvol_rows:
            curr_iv = dvol_rows[-1][1]
            # point le plus proche de 24h en arrière
            target_ms = end_ts - 24 * 3600 * 1000
            prev_row  = min(dvol_rows[:-1], key=lambda r: abs(r[0] - target_ms), default=None)
            if prev_row and abs(prev_row[0] - target_ms) < 3 * 3600 * 1000:
                dvol_1d_chg = round(curr_iv - prev_row[1], 2)
    except Exception:
        pass

    # HV 10j d'il y a 1j (fenêtre décalée) pour le move 1j
    hv_1d_chg = None
    try:
        end_ts_hv   = now_ms()
        start_ts_hv = end_ts_hv - 16 * 24 * 3600 * 1000
        cd = get("get_tradingview_chart_data", {
            "instrument_name": f"{currency}-PERPETUAL",
            "start_timestamp": start_ts_hv,
            "end_timestamp":   end_ts_hv,
            "resolution":      "1D",
        })
        closes = cd.get("close", [])
        if len(closes) >= 12:
            # HV sur fenêtre j-11 à j-1 (= HV 10j d'hier)
            win_prev = closes[-12:-1]
            rets_prev = [math.log(win_prev[j] / win_prev[j-1]) for j in range(1, len(win_prev))]
            hv_prev   = math.sqrt(sum(r**2 for r in rets_prev) / len(rets_prev)) * math.sqrt(365) * 100
            hv_1d_chg = round(hv_10d - hv_prev, 2)
    except Exception:
        pass

    iv_rank   = max(0.0, min(1.0, (curr_iv - iv_min) / max(iv_max - iv_min, 5)))
    iv_hv_ratio = curr_iv / hv_blend if hv_blend > 0 else 1.0

    if curr_iv > 80:
        regime, rec_delta = "HIGH", -0.15
    elif curr_iv > 40:
        regime, rec_delta = "NORMAL", -0.20
    else:
        regime, rec_delta = "LOW", None

    return {
        "hv_10d":      hv_10d,
        "hv_30d":      hv_30d,
        "hv_blend":    hv_blend,
        "hv_1d_chg":   hv_1d_chg,
        "iv_min":      iv_min,
        "iv_max":      iv_max,
        "curr_iv":     curr_iv,
        "dvol_1d_chg": dvol_1d_chg,
        "iv_rank":     iv_rank,
        "iv_hv_ratio": iv_hv_ratio,
        "regime":      regime,
        "rec_delta":   rec_delta,
        "signal_ok":   curr_iv >= 35,
    }


SIZE_CONVEXITY = 1.5   # taille ∝ score^1.5 : concentre le capital sur les meilleurs scores


def compute_sizing(score: float, used_btc: float, iv_rank: float = 1.0) -> float:
    """
    Taille en BTC = score^1.5 × (0.5 + 0.5 × rang DVOL 30j), arrondi à 0.1.

    Le rang DVOL est sorti du score (il était identique pour tous les candidats
    d'un même scan) : le score mesure la qualité de l'option, le rang module
    l'agressivité du sizing (×0.5 en bas de range, ×1.0 en haut).

    La convexité score^1.5 (calibrée sur backtest 4 ans) réduit non-uniformément
    la taille : les setups médiocres (score ≈ seuil 0.45) sont coupés d'un tiers
    (0.45 → 0.30), les bons scores quasi inchangés. Ces setups faibles sont
    surreprésentés les jours fragiles avant les gaps → max drawdown −28% (9.8k→7.1k$
    sur 4 ans) avec un PnL en légère hausse. Voir README et backtest_sizing.py.
    """
    rank_mult = 0.5 + 0.5 * max(0.0, min(1.0, iv_rank))
    raw      = round((score ** SIZE_CONVEXITY) * rank_mult, 1)
    raw      = max(0.1, raw)          # minimum 0.1 BTC
    capacity = max(0.0, MAX_PORTFOLIO_BTC - used_btc)
    return round(min(raw, capacity), 1)


def open_position_from_candidate(row: pd.Series, spot: float, contracts: float = 1.0) -> dict:
    """Crée un dict de position depuis une ligne du DataFrame des candidats."""
    entry_price = float(row["bid_price"]) if row["bid_price"] > 0 else float(row["mark_price"])
    pos = open_position(row, entry_price, contracts, spot)
    pos["entry_score"]   = float(row["score"])
    pos["entry_sizing_btc"] = contracts
    return pos


def scan_entry(currency: str = CURRENCY,
               tte_min: float = SCAN_TTE_MIN, tte_max: float = SCAN_TTE_MAX,
               top_n: int = 8):
    """Scanne les puts OTM et affiche un score d'opportunite composite."""
    print_separator()
    print(f"  Entry Scanner -- {now_dt()}")
    print(f"  TTE [{tte_min:.0f}j - {tte_max:.0f}j]  |  Delta [{SCAN_DELTA_MIN:.2f} - {SCAN_DELTA_MAX:.2f}] (plafond seul)")
    print(f"  Filtre B/A: max {BA_MAX_PCT:.0f}% du mark  |  Prime min: {MIN_PREMIUM_USD:.0f}$/BTC")
    print_separator()

    spot = fetch_spot(currency)
    print(f"\n  Spot {currency}: ${spot:,.2f}")
    print("  Calcul contexte de vol...")
    ctx = get_market_context(currency)

    vrp_lbl = "VRP large -> bon timing" if ctx["iv_hv_ratio"] > 1.3 else ("VRP faible" if ctx["iv_hv_ratio"] < 1.1 else "VRP modere")
    iv_rank_lbl = "IV haute" if ctx["iv_rank"] > 0.6 else ("IV basse" if ctx["iv_rank"] < 0.3 else "IV mediane")
    regime_lbl = {"HIGH": "HIGH [!]", "NORMAL": "NORMAL [~]", "LOW": "LOW [ok]"}.get(ctx["regime"], ctx["regime"])

    print(f"  HV 10j      : {ctx['hv_10d']:.1f}%")
    print(f"  IV range 30j: {ctx['iv_min']:.1f}% - {ctx['iv_max']:.1f}%")
    print(f"  IV ATM ~7j  : {ctx['curr_iv']:.1f}%")
    print(f"  IV/HV ratio : {ctx['iv_hv_ratio']:.2f}x  ({vrp_lbl})")
    print(f"  IV rank 30j : {ctx['iv_rank']*100:.0f}%  ({iv_rank_lbl})")
    print(f"  Regime      : {regime_lbl}")
    if ctx["rec_delta"]:
        print(f"  Delta recom.: {ctx['rec_delta']:+.2f}")
    else:
        print(f"  -> IV trop basse, peu d'edge a vendre de la vol")

    # Charger les instruments déjà en portefeuille pour les exclure du scan
    try:
        _state = load_positions()
        _held = {p["instrument_name"] for p in _state.get("positions", [])}
    except Exception:
        _held = set()

    print(f"\n  Scan des puts OTM en cours...")
    df = fetch_scored_candidates(currency, spot, ctx["hv_blend"], ctx["iv_min"], ctx["iv_max"],
                                  ctx["curr_iv"], tte_min=tte_min, tte_max=tte_max)
    df_all = fetch_scored_candidates(currency, spot, ctx["hv_blend"], ctx["iv_min"], ctx["iv_max"],
                                      ctx["curr_iv"], tte_min=tte_min, tte_max=tte_max, ba_max_pct=999)
    if _held:
        df     = df[~df["instrument_name"].isin(_held)]
        df_all = df_all[~df_all["instrument_name"].isin(_held)]

    if df.empty:
        print("  Aucun candidat trouve (apres filtre B/A et delta).")
        if not df_all.empty:
            print(f"  ({len(df_all)} options trouvees mais toutes rejetees spread B/A>{BA_MAX_PCT:.0f}%)")
        return

    n_rejected = len(df_all) - len(df)
    print_section(f"TOP {min(top_n, len(df))} CANDIDATS (sur {len(df_all)} scannees, {n_rejected} rejetees B/A>{BA_MAX_PCT:.0f}%)")
    print(f"  {'#':<3} {'Instrument':<30} {'TTE':>5} {'Delta':>7} {'IV':>6} "
          f"{'Money':>7} {'Prime$':>8} {'Yield/an':>9} {'B/A%':>6} "
          f"{'SCORE':>7}  IV/HV  Skew  Yield")
    print(f"  {'-'*115}")

    for i, r in df.head(top_n).iterrows():
        bar = "x" * int(r["score"] * 10) + "." * (10 - int(r["score"] * 10))
        print(f"  {i+1:<3} {r['instrument_name']:<30} "
              f"{r['tte_days']:>5.1f}j "
              f"{r['delta']:>+7.3f} "
              f"{r['bid_iv']:>5.1f}% "
              f"{r['moneyness']:>+6.1f}% "
              f"${r['premium_usd']:>7,.0f} "
              f"{r['yield_ann_pct']:>8.1f}% "
              f"{r['ba_pct']:>5.1f}% "
              f"  {r['score']:.3f}  {r['s_iv_hv']:.2f}   {r['s_skew']:.2f}  {r['s_yield']:.2f}  [{bar}]")

    best = df.iloc[0]
    signal_ok = best["score"] >= ENTRY_SCORE_MIN and ctx["signal_ok"]
    print_section("SIGNAL GLOBAL")
    print(f"  Meilleur score : {best['score']:.3f}  {'[OK] OPPORTUNITE' if signal_ok else '[--] ATTENDRE'}")
    print(f"  Seuil score    : {ENTRY_SCORE_MIN:.2f}")
    print(f"  IV/HV ratio    : {ctx['iv_hv_ratio']:.2f}x  (min {ENTRY_IV_HV_MIN:.2f} requis)")
    print(f"  IV courante    : {ctx['curr_iv']:.1f}%  (min 35% requis)")
    if signal_ok:
        print(f"\n  -> Meilleur candidat : {best['instrument_name']}")
        print(f"     Strike {best['strike']:,.0f}  |  TTE {best['tte_days']:.1f}j  |  Delta {best['delta']:+.3f}")
        print(f"     Prime ${best['premium_usd']:,.0f}  |  Yield ann. {best['yield_ann_pct']:.1f}%  |  IV {best['mark_iv']:.1f}%")
        print(f"     B/A spread {best['ba_pct']:.1f}%  |  OI {best['open_interest']:.0f} contrats")
    print_separator()

    # Sauvegarder scan_entry.json pour le dashboard
    try:
        _scan_out = {
            "ts": now_dt(),
            "market_context": {
                "spot": round(spot, 2),
                "hv_10d": round(ctx["hv_10d"], 2),
                "hv_30d": round(ctx["hv_30d"], 2),
                "hv_blend": round(ctx["hv_blend"], 2),
                "hv_1d_chg": ctx.get("hv_1d_chg"),
                "iv_rank": round(ctx["iv_rank"], 3),
                "iv_hv_ratio": round(ctx["iv_hv_ratio"], 3),
                "curr_iv": round(ctx["curr_iv"], 2),
                "dvol_1d_chg": ctx.get("dvol_1d_chg"),
                "regime": ctx["regime"],
                "signal_ok": ctx["signal_ok"],
            },
            "top5": df.head(5).to_dict(orient="records"),
        }
        (Path(__file__).parent / "scan_entry.json").write_text(
            json.dumps(_scan_out, indent=2, default=str)
        )
        print(f"  scan_entry.json sauvegarde ({len(df.head(5))} candidats)")
    except Exception as _e:
        print(f"  [warn] scan_entry.json non sauvegarde: {_e}")


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
    parser.add_argument("--reset",      action="store_true",
                        help="Remet a zero les positions sauvegardees")
    parser.add_argument("--scan-entry", action="store_true",
                        help="Scanner les opportunités d'entrée (score VRP)")
    parser.add_argument("--tte-min",    type=float, default=5.0)
    parser.add_argument("--tte-max",    type=float, default=30.0)
    args = parser.parse_args()

    if args.reset:
        POSITIONS_FILE.unlink(missing_ok=True)
        print("Positions remises a zero.")
    elif getattr(args, "scan_entry", False):
        scan_entry(args.currency, tte_min=args.tte_min, tte_max=args.tte_max)
    elif args.monitor:
        monitor_loop(args.interval, args.currency)
    else:
        run_once(args.currency)
