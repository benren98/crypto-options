"""
PnL Monitor — Short Put VRP Strategy
======================================
Suit en temps réel (ou sur snapshot) la position short put :

  - PnL option MtM (mark-to-market en BTC et USD)
  - PnL hedge perp (delta hedge short)
  - Funding cost du perp
  - Theta théorique encaissé vs PnL réel (VRP capture check)
  - Historique des snapshots pour graphiques

Usage :
    python pnl_monitor.py                # snapshot unique + affichage
    python pnl_monitor.py --watch 5      # refresh toutes les 5 minutes
    python pnl_monitor.py --plot         # affiche les graphiques de PnL
    python pnl_monitor.py --report       # résumé complet en CSV
"""

import argparse
import json
import math
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

from gist_sync import push_positions

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
import requests
from scipy.stats import norm
from urllib3.exceptions import InsecureRequestWarning

warnings.filterwarnings("ignore", category=InsecureRequestWarning)

BASE_URL       = "https://www.deribit.com/api/v2/public"
POSITIONS_FILE = Path(__file__).parent / "positions.json"
OUTPUT_DIR     = Path(__file__).parent / "output"
SNAPSHOTS_FILE = OUTPUT_DIR / "pnl_snapshots.csv"
OUTPUT_DIR.mkdir(exist_ok=True)

FUNDING_APPROX_DAILY = 0.0001   # ~0.01%/jour si funding indisponible (fallback)


# ── API ───────────────────────────────────────────────────────────────────────

def get(method: str, params: dict) -> dict:
    r = requests.get(f"{BASE_URL}/{method}", params=params,
                     timeout=15, verify=False)
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        raise RuntimeError(f"API: {d['error']}")
    return d["result"]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_str() -> str:
    return now_utc().strftime("%Y-%m-%d %H:%M:%S UTC")


# ── Market data ───────────────────────────────────────────────────────────────

def fetch_spot(currency: str = "BTC") -> float:
    return get("get_index_price", {"index_name": f"{currency.lower()}_usd"})["index_price"]


def fetch_option_ticker(instrument_name: str) -> dict:
    return get("ticker", {"instrument_name": instrument_name})


def fetch_perp_funding(currency: str = "BTC") -> float:
    """Retourne le funding rate actuel du perp (en fraction, par 8h)."""
    try:
        data = get("get_funding_rate_value", {
            "instrument_name": f"{currency}-PERPETUAL",
            "start_timestamp": int((now_utc().timestamp() - 28800) * 1000),
            "end_timestamp":   int(now_utc().timestamp() * 1000),
        })
        return float(data) if data else FUNDING_APPROX_DAILY / 3
    except Exception:
        return FUNDING_APPROX_DAILY / 3   # 3 périodes/jour


def fetch_perp_mark(currency: str = "BTC") -> float:
    try:
        t = get("ticker", {"instrument_name": f"{currency}-PERPETUAL"})
        return t.get("mark_price", fetch_spot(currency))
    except Exception:
        return fetch_spot(currency)


# ── BS helpers ────────────────────────────────────────────────────────────────

def bs_put_price(S, K, T, sigma, r=0.05) -> float:
    if T <= 0:
        return max(K - S, 0) / S  # intrinsic, en fraction
    d1 = (np.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*np.sqrt(T))
    d2 = d1 - sigma*np.sqrt(T)
    from scipy.stats import norm
    price_usd = K*np.exp(-r*T)*norm.cdf(-d2) - S*norm.cdf(-d1)
    return price_usd / S   # en fraction de BTC (comme Deribit)


def bs_theta_btc(S, K, T, sigma, r=0.05) -> float:
    """Theta journalier en fraction de BTC."""
    if T <= 1e-6:
        return 0.0
    d1 = (np.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*np.sqrt(T))
    d2 = d1 - sigma*np.sqrt(T)
    phi = norm.pdf(d1)
    theta_usd = (-(S * phi * sigma) / (2*np.sqrt(T))
                 + r*K*np.exp(-r*T)*norm.cdf(-d2)) / 365
    return theta_usd / S


# ── PnL calculator ────────────────────────────────────────────────────────────

def compute_snapshot(position: dict) -> dict:
    """Calcule tous les composants du PnL pour la position courante."""
    instr   = position["instrument_name"]
    strike  = position["strike"]
    entry_p = position["entry_price"]       # en BTC
    entry_s = position["entry_spot"]        # spot USD à l'entrée
    contracts = position["contracts"]
    hedge_qty = position.get("hedge_qty", 0.0)   # BTC shortés sur perp (négatif)
    # hedge_avg_entry = VWAP du hedge après rebalancements successifs
    # Fallback sur hedge_entry_spot (1er hedge) puis entry_spot
    hedge_entry_spot = position.get("hedge_avg_entry",
                       position.get("hedge_entry_spot", entry_s))

    # Data live
    ticker     = fetch_option_ticker(instr)
    spot       = ticker.get("underlying_price") or fetch_spot()
    curr_mark  = ticker.get("mark_price", entry_p)
    curr_bid   = ticker.get("best_bid_price") or curr_mark
    curr_ask   = ticker.get("best_ask_price") or curr_mark
    # MtM au mark price (référence neutre) — le ask est affiché séparément
    # comme "coût de sortie" mais ne doit pas driver le PnL live
    curr_p     = curr_mark
    curr_iv    = ticker.get("mark_iv", position["iv_at_entry"])
    greeks     = ticker.get("greeks") or {}
    curr_delta = greeks.get("delta", position["delta_at_entry"])
    curr_gamma = greeks.get("gamma", position.get("gamma_at_entry", 7e-5))
    curr_vega  = greeks.get("vega", position.get("vega_at_entry", 13.0))
    curr_theta = greeks.get("theta", 0)   # USD/jour (Deribit convention — déjà normalisé)

    # Seuil de rebalancement dynamique (même formule que greeks_hedge.py)
    HEDGE_THRESHOLD_BASE_PCT = 5.0
    HEDGE_IV_REF = 70.0
    iv_scale         = math.sqrt(max(curr_iv, 20.0) / HEDGE_IV_REF)
    hedge_thr_pct    = max(2.0, min(8.0, HEDGE_THRESHOLD_BASE_PCT * iv_scale))
    hedge_thr_btc    = hedge_thr_pct / 100.0

    # TTE en jours
    expiry_dt = pd.to_datetime(position["expiry_dt"], utc=True)
    tte_days  = (expiry_dt - pd.Timestamp(now_utc())).total_seconds() / 86400
    tte_years = max(tte_days / 365, 1e-6)

    # ── PnL option (short put) ────────────────────────────────────────────────
    # On a vendu à entry_p, valeur actuelle est curr_p
    pnl_opt_btc = (entry_p - curr_p) * contracts
    pnl_opt_usd = pnl_opt_btc * spot

    # ── PnL hedge perp (short perp = hedge_qty négatif) ──────────────────────
    # hedge_qty < 0 = on a shorté |hedge_qty| BTC de perp
    # PnL short perp = -hedge_qty * (spot_now - spot_entry)
    # Short perp : hedge_qty est négatif (ex: -0.188 = 0.188 BTC shortés)
    # PnL short = qty × (entry - spot) = hedge_qty × (spot - entry) car qty < 0
    # Si spot baisse → (spot - entry) < 0 → négatif × négatif = positif ✅
    pnl_hedge_mtm  = hedge_qty * (spot - hedge_entry_spot)
    # Ajouter le PnL réalisé sur les rachats partiels passés (tracked par greeks_hedge.py)
    realized_hedge = float(position.get("realized_hedge_pnl_usd", 0.0))
    pnl_hedge_usd  = pnl_hedge_mtm + realized_hedge

    # ── Funding cost du perp ──────────────────────────────────────────────────
    # On est short perp -> si funding > 0 on REÇOIT le funding
    funding_8h     = fetch_perp_funding()
    funding_daily  = funding_8h * 3              # 3 périodes de 8h par jour
    # Durée de la position en jours
    entry_dt       = pd.to_datetime(position["entry_ts"].replace(" UTC",""),
                                    format="%Y-%m-%d %H:%M:%S",
                                    utc=True)
    days_held      = (pd.Timestamp(now_utc()) - entry_dt).total_seconds() / 86400
    # Valeur notionnelle du hedge
    perp_mark      = fetch_perp_mark()
    hedge_notional = abs(hedge_qty) * perp_mark
    funding_pnl_usd= funding_daily * days_held * hedge_notional
    # Short perp : si funding_rate > 0, les longs paient les shorts -> on reçoit
    # Convention Deribit : funding_rate > 0 = longs paient courts -> short reçoit

    # ── Theta théorique encaissé ──────────────────────────────────────────────
    # theta_at_entry en BTC/jour -> convertir en USD/jour puis multiplier par days_held
    theta_entry_btc  = abs(position.get("theta_at_entry", 0))   # BTC/jour (BS brut)
    theta_entry_usd  = theta_entry_btc * entry_s                 # USD/jour à l'entrée
    theta_theory_usd = theta_entry_usd * days_held               # USD encaissé théorique

    # Deribit donne theta en USD/jour directement (déjà normalisé)
    theta_deribit_usd = abs(curr_theta) if curr_theta else 0.0

    # ── PnL total ─────────────────────────────────────────────────────────────
    total_pnl_usd = pnl_opt_usd + pnl_hedge_usd + funding_pnl_usd

    # ── VRP capture ratio ─────────────────────────────────────────────────────
    # Ratio PnL réel / theta théorique - significatif seulement si on a tenu ≥ 6h
    # Avant ça, le ratio est bruité (petit dénominateur)
    if theta_theory_usd > 0.5 and days_held > 0.25:
        vrp_capture_pct = pnl_opt_usd / theta_theory_usd * 100
    else:
        vrp_capture_pct = float("nan")  # trop tôt pour mesurer

    return {
        "timestamp":          now_str(),
        "tte_days":           round(tte_days, 4),
        "days_held":          round(days_held, 4),
        "spot":               round(spot, 2),
        "entry_spot":         entry_s,
        "spot_move_pct":      round((spot / entry_s - 1) * 100, 3),
        # Option
        "entry_price_btc":    entry_p,
        "current_price_btc":  round(curr_p, 6),
        "current_iv_pct":     curr_iv,
        "entry_iv_pct":       position["iv_at_entry"],
        "iv_change":          round(curr_iv - position["iv_at_entry"], 2),
        "current_delta":      curr_delta,
        # PnL components
        "pnl_option_btc":     round(pnl_opt_btc, 6),
        "pnl_option_usd":     round(pnl_opt_usd, 2),
        "pnl_hedge_usd":      round(pnl_hedge_usd, 2),
        "funding_pnl_usd":    round(funding_pnl_usd, 4),
        "total_pnl_usd":      round(total_pnl_usd, 2),
        "pnl_pct_of_premium": round(pnl_opt_usd / (entry_p * entry_s) * 100, 2),
        # Theta analysis
        "theta_theory_usd":   round(theta_theory_usd, 2),
        "theta_daily_now_usd":round(theta_deribit_usd, 2),
        "vrp_capture_pct":    round(vrp_capture_pct, 1),
        # Greeks live
        "live_delta":         round(curr_delta, 5),
        "live_gamma":         round(curr_gamma, 7),
        "hedge_qty":          hedge_qty,
        "hedge_delta_drift":  round(-curr_delta - abs(hedge_qty), 5),
        "hedge_threshold_pct": round(hedge_thr_pct, 2),
        "hedge_threshold_btc": round(hedge_thr_btc, 5),
        # Données pour attribution
        "current_price_btc":  round(curr_mark, 6),   # mark pour attribution mid-to-mid
        "current_ask_btc":    round(curr_ask,  6),
        "current_bid_btc":    round(curr_bid,  6),
        "entry_iv_pct":       position["iv_at_entry"],
        "live_vega":          round(curr_vega, 4),
    }


# ── PnL Attribution (Greek explain) ──────────────────────────────────────────

def compute_pnl_attribution(snap: dict, position: dict) -> dict:
    """
    Décompose le PnL option en contributions par Greek + bid-ask.

    Structure de la décomposition (tout en USD) :
    ┌─────────────────────────────────────────────────────┐
    │  PnL_total = PnL_mid + BA_entry + BA_exit           │
    │  PnL_mid   ≈ Delta + Gamma + Theta + Vega + Résidu  │
    └─────────────────────────────────────────────────────┘
    """
    spot        = snap["spot"]
    entry_spot  = position["entry_spot"]
    entry_bid   = position["entry_price"]          # prix vendu (bid à l'entrée)
    entry_mark  = position.get("entry_mark_price", entry_bid)  # mark à l'entrée
    curr_mark   = snap["current_price_btc"]        # mark actuel (bid utilisé comme proxy)
    curr_ask    = snap.get("current_ask_btc", curr_mark + 0.0005)  # ask actuel

    # --- Bid-ask costs -------------------------------------------------------
    # Coût à l'entrée : on a vendu au bid au lieu du mid
    ba_entry_usd = -(entry_mark - entry_bid) * entry_spot          # négatif = coût
    # Coût à la sortie : on rachèterait à l'ask au lieu du mid
    ba_exit_usd  = -(curr_ask   - curr_mark) * spot                # négatif = coût

    # --- Mid-to-mid PnL -------------------------------------------------------
    # Variation de valeur au mark price (hors spread)
    mid_to_mid_usd = (entry_mark - curr_mark) * spot               # short = on veut que curr < entry

    # --- Greek attribution du mid-to-mid -------------------------------------
    delta_live   = snap["live_delta"]              # delta de l'option (négatif pour put)
    pos_delta    = -delta_live                     # position short -> inversé
    delta_spot   = spot - entry_spot               # variation spot

    # Récupère les Greeks depuis positions (entrée) et greeks live du snap
    gamma_entry  = position.get("gamma_at_entry", 0.00007)
    # vega_live du snap (négatif car short, en USD par 1pt IV)
    vega_live    = -abs(snap.get("live_vega", position.get("vega_at_entry", 13.0)))
    theta_daily  = snap["theta_daily_now_usd"]    # USD/jour (positif pour short put)

    hours_held   = snap["days_held"] * 24
    delta_t_days = snap["days_held"]
    delta_iv     = snap["current_iv_pct"] - snap["entry_iv_pct"]   # variation IV en pts

    # Contributions (position short -> on inverse le signe des Greeks option)
    pnl_delta    = pos_delta * delta_spot                          # USD
    pnl_gamma    = 0.5 * (-gamma_entry) * (delta_spot ** 2)       # short gamma = négatif si move
    pnl_theta    = theta_daily * delta_t_days                      # positif (on encaisse)
    pnl_vega     = vega_live * delta_iv                            # négatif si IV monte

    greek_total  = pnl_delta + pnl_gamma + pnl_theta + pnl_vega
    residual     = mid_to_mid_usd - greek_total                    # higher order + model error

    # --- Réconciliation totale -----------------------------------------------
    # MtM avec ask pour sortie = mid_to_mid + ba_entry + ba_exit
    recon_total  = mid_to_mid_usd + ba_entry_usd + ba_exit_usd

    return {
        "pnl_delta":      round(pnl_delta,    2),
        "pnl_gamma":      round(pnl_gamma,    2),
        "pnl_theta":      round(pnl_theta,    2),
        "pnl_vega":       round(pnl_vega,     2),
        "pnl_greek_total":round(greek_total,  2),
        "pnl_residual":   round(residual,     2),
        "mid_to_mid_usd": round(mid_to_mid_usd, 2),
        "ba_entry_usd":   round(ba_entry_usd, 2),
        "ba_exit_usd":    round(ba_exit_usd,  2),
        "recon_total":    round(recon_total,  2),
        "hours_held":     round(hours_held,   2),
        "delta_spot":     round(delta_spot,   2),
        "delta_iv":       round(delta_iv,     2),
    }


# ── Display ───────────────────────────────────────────────────────────────────

SEP = "=" * 62

def color_sign(val: float, fmt: str = ".2f") -> str:
    """Préfixe + si positif, affiche 0.00 si nul."""
    s = f"{val:{fmt}}"
    return f"+{s}" if val > 0 else s


def display_snapshot(snap: dict, position: dict):
    print(f"\n{SEP}")
    print(f"  PnL Monitor — {snap['timestamp']}")
    print(SEP)

    print(f"\n  Position  : SHORT {position['instrument_name']}")
    print(f"  Strike    : {position['strike']:,.0f}  |  "
          f"Expiry: {position['expiry_dt'][:10]}")
    print(f"  TTE       : {snap['tte_days']:.3f} jours")
    print(f"  Tenu depuis : {snap['days_held']*24:.1f}h ({snap['days_held']:.3f}j)")

    # Prix
    print(f"\n  {'':─<58}")
    print(f"  {'PRIX':}")
    print(f"  Spot          : ${snap['spot']:>12,.2f}  "
          f"(entrée: ${snap['entry_spot']:,.2f}  "
          f"{color_sign(snap['spot_move_pct'], '.2f')}%)")
    print(f"  Option entry  : {snap['entry_price_btc']:.5f} BTC  "
          f"(${snap['entry_price_btc']*snap['entry_spot']:,.2f})")
    print(f"  Option actuel : {snap['current_price_btc']:.5f} BTC  "
          f"(${snap['current_price_btc']*snap['spot']:,.2f})")
    print(f"  IV entry      : {snap['entry_iv_pct']:.1f}%  ->  "
          f"actuelle: {snap['current_iv_pct']:.1f}%  "
          f"({color_sign(snap['iv_change'], '.1f')} pts)")

    # PnL
    print(f"\n  {'':─<58}")
    print(f"  {'PnL COMPOSANTS':}")
    w = 20
    print(f"  {'Option (MtM)':<{w}} : {color_sign(snap['pnl_option_usd']):>10} USD  "
          f"  ({color_sign(snap['pnl_pct_of_premium'])}% de la prime)")
    print(f"  {'Hedge perp':<{w}} : {color_sign(snap['pnl_hedge_usd']):>10} USD")
    print(f"  {'Funding perp':<{w}} : {color_sign(snap['funding_pnl_usd']):>10} USD")
    print(f"  {'─'*40}")
    total_sign = "+" if snap['total_pnl_usd'] >= 0 else ""
    print(f"  {'TOTAL':<{w}} : {total_sign}{snap['total_pnl_usd']:>10.2f} USD")

    # PnL Attribution
    attr = compute_pnl_attribution(snap, position)
    print(f"\n  {'':─<58}")
    print(f"  PnL ATTRIBUTION  (ΔSpot={attr['delta_spot']:+.0f}$  ΔIV={attr['delta_iv']:+.1f}pts"
          f"  tenu={attr['hours_held']:.1f}h)")
    print(f"  {'─'*56}")
    print(f"  {'Delta':<18} : {color_sign(attr['pnl_delta']):>9} USD"
          f"   (pos +{abs(snap['live_delta']):.3f} × ΔSpot {attr['delta_spot']:+.0f}$)")
    print(f"  {'Gamma':<18} : {color_sign(attr['pnl_gamma']):>9} USD"
          f"   (short gamma, ΔSpot²)")
    print(f"  {'Theta':<18} : {color_sign(attr['pnl_theta']):>9} USD"
          f"   ({attr['hours_held']:.1f}h × ${snap['theta_daily_now_usd']:.2f}/j)")
    print(f"  {'Vega':<18} : {color_sign(attr['pnl_vega']):>9} USD"
          f"   (ΔIV {attr['delta_iv']:+.1f}pts × vega {attr.get('pnl_vega')/attr['delta_iv']:.1f})"
          if attr['delta_iv'] != 0 else f"  {'Vega':<18} : {color_sign(attr['pnl_vega']):>9} USD")
    print(f"  {'Résidu (HO)':<18} : {color_sign(attr['pnl_residual']):>9} USD")
    print(f"  {'─'*38}")
    print(f"  {'Mid-to-mid total':<18} : {color_sign(attr['mid_to_mid_usd']):>9} USD")
    print(f"  {'':─<56}")
    print(f"  {'Bid-ask entrée':<18} : {color_sign(attr['ba_entry_usd']):>9} USD"
          f"   (vendu bid {position['entry_price']:.4f} vs mark {position.get('entry_mark_price',0):.4f})")
    print(f"  {'Bid-ask sortie':<18} : {color_sign(attr['ba_exit_usd']):>9} USD"
          f"   (rachat ask {snap['current_ask_btc']:.4f} vs mark {snap['current_price_btc']:.4f})")
    print(f"  {'─'*38}")
    print(f"  {'TOTAL OPTION':<18} : {color_sign(attr['recon_total']):>9} USD")

    # Theta
    print(f"\n  {'':─<58}")
    print(f"  {'THETA / VRP':}")
    print(f"  Theta theorie (cum.) : ${snap['theta_theory_usd']:>8.2f} USD  "
          f"({snap['days_held']*24:.1f}h de decay)")
    print(f"  Theta actuel (daily) : ${snap['theta_daily_now_usd']:>8.2f} USD/jour")
    cap = snap['vrp_capture_pct']
    import math as _math
    if _math.isnan(cap):
        print(f"  VRP capture          : (trop tot — attendre ≥6h de holding)")
    else:
        bar_len = min(int(abs(cap) / 5), 20)
        bar = "#" * bar_len + "." * (20 - bar_len)
        print(f"  VRP capture          : {color_sign(cap, '.1f')}%  [{bar}]")
        if cap < 50:
            print(f"  >> IV en hausse ou mouvement du spot absorbe le theta")
        elif cap > 150:
            print(f"  >> Excellent: theta encaisse + compression IV")

    # Greeks live + hedge
    print(f"\n  {'':─<58}")
    print(f"  {'DELTA / HEDGE':}")
    print(f"  Delta live   : {snap['live_delta']:+.5f}  "
          f"(entrée: {position['delta_at_entry']:+.4f})")
    print(f"  Hedge actuel : {snap['hedge_qty']:+.5f} BTC short perp")
    drift     = snap['hedge_delta_drift']
    thr_btc   = snap.get('hedge_threshold_btc', 0.03)
    thr_pct   = snap.get('hedge_threshold_pct', 3.0)
    drift_pct = abs(drift) * 100
    drift_flag = "  *** REBALANCER ***" if abs(drift) > thr_btc else "  OK"
    print(f"  Drift delta  : {drift:+.5f} ({drift_pct:.2f}%){drift_flag}")
    print(f"  Seuil IV-adj : {thr_pct:.1f}% delta = {thr_btc:.5f} BTC")

    # Alertes
    alerts = []
    if snap['tte_days'] < 1.0:
        alerts.append(f"  [ROLL NOW] TTE={snap['tte_days']:.2f}j < 1j -> fermer et roller")
    elif snap['tte_days'] < 1.5:
        alerts.append(f"  [ROLL SOON] TTE={snap['tte_days']:.2f}j -> preparer le roll")
    if abs(snap['spot_move_pct']) > 3:
        alerts.append(f"  [MOUVEMENT] Spot {color_sign(snap['spot_move_pct'])}% depuis l'entree")
    if snap['iv_change'] > 10:
        alerts.append(f"  [IV SPIKE] IV +{snap['iv_change']:.1f} pts -> vega loss")
    if snap['pnl_option_usd'] < -(snap['entry_price_btc'] * snap['entry_spot']):
        alerts.append(f"  [STOP LOSS] PnL > -100% de la prime -> envisager de couper")

    if alerts:
        print(f"\n  {'':─<58}")
        print(f"  ALERTES:")
        for a in alerts:
            print(a)

    print(f"\n{SEP}\n")


# ── Snapshots persistence ─────────────────────────────────────────────────────

def save_snapshot(snap: dict):
    """Append le snapshot au CSV historique."""
    df_new = pd.DataFrame([snap])
    if SNAPSHOTS_FILE.exists():
        df_old = pd.read_csv(SNAPSHOTS_FILE)
        df = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df = df_new
    df.to_csv(SNAPSHOTS_FILE, index=False)


def load_snapshots() -> pd.DataFrame:
    if SNAPSHOTS_FILE.exists():
        df = pd.read_csv(SNAPSHOTS_FILE)
        df["timestamp"] = pd.to_datetime(df["timestamp"].str.replace(" UTC",""),
                                          format="%Y-%m-%d %H:%M:%S", utc=True)
        return df
    return pd.DataFrame()


# ── Plots ─────────────────────────────────────────────────────────────────────

def plot_pnl(snapshots: pd.DataFrame, position: dict):
    if snapshots.empty or len(snapshots) < 2:
        print("  Pas assez de snapshots pour tracer (minimum 2 requis).")
        return

    fig, axes = plt.subplots(2, 2, figsize=(14, 9))
    fig.suptitle(
        f"PnL Monitor — {position['instrument_name']}\n"
        f"Short {position['contracts']}x {position['strike']:,.0f} Put  |  "
        f"Entry: {position['entry_price']:.5f} BTC @ ${position['entry_spot']:,.0f}",
        fontsize=12
    )
    ts = snapshots["timestamp"]

    # ── 1. PnL total + composants ─────────────────────────────────────────────
    ax = axes[0, 0]
    ax.fill_between(ts, snapshots["total_pnl_usd"], 0,
                    where=(snapshots["total_pnl_usd"] >= 0),
                    alpha=0.3, color="#4CAF50", label="_nolegend_")
    ax.fill_between(ts, snapshots["total_pnl_usd"], 0,
                    where=(snapshots["total_pnl_usd"] < 0),
                    alpha=0.3, color="#F44336", label="_nolegend_")
    ax.plot(ts, snapshots["total_pnl_usd"], color="#212121",
            linewidth=2, label="PnL Total")
    ax.plot(ts, snapshots["pnl_option_usd"], "--",
            color="#2196F3", linewidth=1.5, label="Option MtM")
    ax.plot(ts, snapshots["pnl_hedge_usd"], "--",
            color="#FF9800", linewidth=1.5, label="Hedge Perp")
    ax.axhline(0, color="black", linewidth=0.7)
    max_loss = -(position["entry_price"] * position["entry_spot"])
    ax.axhline(max_loss, color="#F44336", linestyle=":", linewidth=1,
               label=f"Max loss ({max_loss:.0f}$)")
    ax.set_title("PnL Composants (USD)")
    ax.set_ylabel("USD")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m %H:%M"))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, fontsize=7)

    # ── 2. Theta théorique vs PnL réel ───────────────────────────────────────
    ax = axes[0, 1]
    ax.plot(ts, snapshots["theta_theory_usd"], color="#9C27B0",
            linewidth=2, linestyle="--", label="Theta cumulatif (theorie)")
    ax.plot(ts, snapshots["pnl_option_usd"], color="#2196F3",
            linewidth=2, label="PnL Option reel")
    ax.fill_between(ts,
                    snapshots["theta_theory_usd"],
                    snapshots["pnl_option_usd"],
                    alpha=0.15, color="#FF5722",
                    label="Ecart (VRP non capture)")
    ax.axhline(0, color="black", linewidth=0.7)
    ax.set_title("Theta Theorique vs PnL Reel")
    ax.set_ylabel("USD")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m %H:%M"))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, fontsize=7)

    # ── 3. Spot + IV ──────────────────────────────────────────────────────────
    ax  = axes[1, 0]
    ax2 = ax.twinx()
    ax.plot(ts, snapshots["spot"], color="#1565C0", linewidth=2, label="Spot BTC")
    ax.axhline(position["strike"], color="#F44336", linestyle=":",
               linewidth=1.5, label=f"Strike {position['strike']:,.0f}")
    ax.axhline(position["entry_spot"], color="#4CAF50", linestyle="--",
               linewidth=1, label=f"Entry spot {position['entry_spot']:,.0f}")
    ax2.plot(ts, snapshots["current_iv_pct"], color="#FF6F00",
             linewidth=1.5, linestyle="--", label="IV (%)")
    ax2.axhline(position["iv_at_entry"], color="#FF6F00", linestyle=":",
                linewidth=1, alpha=0.5)
    ax.set_title("Spot BTC & Implied Vol")
    ax.set_ylabel("Spot (USD)", color="#1565C0")
    ax2.set_ylabel("IV (%)", color="#FF6F00")
    lines1, labels1 = ax.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(lines1 + lines2, labels1 + labels2, fontsize=8)
    ax.grid(alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m %H:%M"))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, fontsize=7)

    # ── 4. Delta drift + VRP capture ─────────────────────────────────────────
    ax = axes[1, 1]
    ax.bar(ts, snapshots["vrp_capture_pct"], color=[
        "#4CAF50" if v >= 0 else "#F44336"
        for v in snapshots["vrp_capture_pct"]
    ], alpha=0.6, width=0.01, label="VRP capture %")
    ax.axhline(100, color="#9C27B0", linestyle="--",
               linewidth=1.5, label="100% (parfait)")
    ax.axhline(0, color="black", linewidth=0.7)

    ax2 = ax.twinx()
    ax2.plot(ts, snapshots["live_delta"], color="#FF9800",
             linewidth=1.5, label="Delta live")
    ax2.axhline(position["delta_at_entry"], color="#FF9800",
                linestyle=":", linewidth=1, alpha=0.5)
    ax2.set_ylabel("Delta", color="#FF9800")

    ax.set_title("VRP Capture % & Delta")
    ax.set_ylabel("VRP capture (%)")
    lines1, labels1 = ax.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(lines1 + lines2, labels1 + labels2, fontsize=8)
    ax.grid(alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m %H:%M"))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, fontsize=7)

    fig.tight_layout()
    path = OUTPUT_DIR / "pnl_dashboard.png"
    fig.savefig(path, dpi=150)
    print(f"  [plot] {path}")
    plt.close(fig)


# ── Report CSV ────────────────────────────────────────────────────────────────

def print_report(snapshots: pd.DataFrame, position: dict):
    if snapshots.empty:
        print("  Aucun snapshot enregistre.")
        return
    last = snapshots.iloc[-1]
    first = snapshots.iloc[0]
    print(f"\n{'='*62}")
    print(f"  RAPPORT COMPLET — {position['instrument_name']}")
    print(f"{'='*62}")
    print(f"  Snapshots enregistres : {len(snapshots)}")
    print(f"  Periode               : {first['timestamp']} -> {last['timestamp']}")
    print(f"\n  PnL Max      : ${snapshots['total_pnl_usd'].max():>10.2f}")
    print(f"  PnL Min      : ${snapshots['total_pnl_usd'].min():>10.2f}")
    print(f"  PnL actuel   : ${last['total_pnl_usd']:>10.2f}")
    print(f"\n  IV range     : {snapshots['current_iv_pct'].min():.1f}% - "
          f"{snapshots['current_iv_pct'].max():.1f}%")
    print(f"  Spot range   : ${snapshots['spot'].min():,.0f} - "
          f"${snapshots['spot'].max():,.0f}")
    print(f"\n  Theta theorie cum. : ${last['theta_theory_usd']:.2f}")
    print(f"  VRP capture moyen  : {snapshots['vrp_capture_pct'].mean():.1f}%")
    print(f"{'='*62}")


# ── Main ──────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    """Charge positions.json — supporte l'ancien format (open:dict) et le nouveau (positions:list)."""
    if not POSITIONS_FILE.exists():
        return {"positions": [], "hedge": {}}
    state = json.loads(POSITIONS_FILE.read_text())
    # Migration ancien format
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
    return state


def compute_option_pnl(position: dict, spot: float) -> dict:
    """Calcule le PnL option (MtM) et les Greeks live pour une position, sans hedge."""
    instr      = position["instrument_name"]
    entry_p    = position["entry_price"]
    entry_s    = position["entry_spot"]
    contracts  = position["contracts"]

    ticker     = fetch_option_ticker(instr)
    curr_mark  = ticker.get("mark_price", entry_p)
    curr_bid   = ticker.get("best_bid_price") or curr_mark
    curr_ask   = ticker.get("best_ask_price") or curr_mark
    curr_iv    = ticker.get("mark_iv", position["iv_at_entry"])
    greeks     = ticker.get("greeks") or {}
    curr_delta = greeks.get("delta", position["delta_at_entry"])
    curr_gamma = greeks.get("gamma", position.get("gamma_at_entry", 7e-5))
    curr_vega  = greeks.get("vega",  position.get("vega_at_entry", 13.0))
    curr_theta = greeks.get("theta", 0)

    expiry_dt = pd.to_datetime(position["expiry_dt"], utc=True)
    tte_days  = (expiry_dt - pd.Timestamp(now_utc())).total_seconds() / 86400
    entry_dt  = pd.to_datetime(position["entry_ts"].replace(" UTC", ""),
                               format="%Y-%m-%d %H:%M:%S", utc=True)
    days_held = (pd.Timestamp(now_utc()) - entry_dt).total_seconds() / 86400

    pnl_opt_btc = (entry_p - curr_mark) * contracts
    pnl_opt_usd = pnl_opt_btc * spot

    theta_entry_btc = abs(position.get("theta_at_entry", 0))
    theta_theory_usd = theta_entry_btc * entry_s * days_held
    theta_deribit_usd = abs(curr_theta) if curr_theta else 0.0

    vrp_capture_pct = (pnl_opt_usd / theta_theory_usd * 100
                       if theta_theory_usd > 0.5 and days_held > 0.25
                       else float("nan"))

    return {
        "instrument":         instr,
        "tte_days":           round(tte_days, 4),
        "days_held":          round(days_held, 4),
        "entry_spot":         entry_s,
        "entry_price_btc":    entry_p,
        "current_price_btc":  round(curr_mark, 6),
        "current_ask_btc":    round(curr_ask,  6),
        "current_bid_btc":    round(curr_bid,  6),
        "current_iv_pct":     curr_iv,
        "entry_iv_pct":       position["iv_at_entry"],
        "iv_change":          round(curr_iv - position["iv_at_entry"], 2),
        "pnl_option_btc":     round(pnl_opt_btc, 6),
        "pnl_option_usd":     round(pnl_opt_usd, 2),
        "pnl_pct_of_premium": round(pnl_opt_usd / (entry_p * entry_s) * 100, 2),
        "theta_theory_usd":   round(theta_theory_usd, 2),
        "theta_daily_now_usd":round(theta_deribit_usd, 2),
        "vrp_capture_pct":    round(vrp_capture_pct, 1),
        "live_delta":         round(curr_delta, 5),
        "live_gamma":         round(curr_gamma, 7),
        "live_vega":          round(curr_vega,  4),
    }


def compute_portfolio_snapshot(state: dict) -> dict:
    """Calcule le snapshot portfolio : option PnL par position + hedge PnL partagé."""
    positions = state.get("positions", [])
    hedge     = state.get("hedge", {})

    if not positions:
        return {}

    spot         = fetch_spot()
    funding_8h   = fetch_perp_funding()
    funding_daily = funding_8h * 3
    perp_mark    = fetch_perp_mark()

    hedge_qty         = float(hedge.get("qty", 0.0))
    hedge_avg         = float(hedge.get("avg_entry", spot))
    realized_hedge    = float(hedge.get("realized_pnl_usd", 0.0))

    # Option PnL par position
    pos_snaps = [compute_option_pnl(p, spot) for p in positions]

    # Greeks cumulés du portefeuille
    net_delta   = sum(s["live_delta"] for s in pos_snaps)
    net_gamma   = sum(s["live_gamma"] for s in pos_snaps)
    net_vega    = sum(s["live_vega"]  for s in pos_snaps)
    net_theta   = sum(s["theta_daily_now_usd"] for s in pos_snaps)
    net_theta_theory = sum(s["theta_theory_usd"] for s in pos_snaps)

    # Hedge PnL
    pnl_hedge_mtm = hedge_qty * (spot - hedge_avg)
    pnl_hedge_usd = pnl_hedge_mtm + realized_hedge

    # Funding (sur notionnel total du hedge)
    days_held_max = max(s["days_held"] for s in pos_snaps)
    hedge_notional = abs(hedge_qty) * perp_mark
    funding_pnl_usd = funding_daily * days_held_max * hedge_notional

    # PnL option total
    pnl_option_total = sum(s["pnl_option_usd"] for s in pos_snaps)
    total_pnl_usd    = pnl_option_total + pnl_hedge_usd + funding_pnl_usd

    # Seuil hedge dynamique basé sur IV de la position principale (la plus longue)
    primary = max(pos_snaps, key=lambda s: s["tte_days"])
    iv_scale      = math.sqrt(max(primary["current_iv_pct"], 20.0) / 70.0)
    hedge_thr_pct = max(2.0, min(8.0, 5.0 * iv_scale))
    hedge_thr_btc = hedge_thr_pct / 100.0

    # Drift = (delta net des options + hedge) = ce qu'il reste non couvert
    # net_delta < 0 pour puts (holders), -net_delta = position delta du vendeur
    hedge_delta_drift = round(-net_delta - abs(hedge_qty), 5)

    # Snap primaire = position avec TTE max (pour affichage principal)
    p0 = primary

    # VRP capture portfolio
    pnl_opt_total_check = pnl_option_total
    vrp_portfolio = (pnl_opt_total_check / net_theta_theory * 100
                     if net_theta_theory > 0.5 else float("nan"))

    # Snap principal = référence position la plus longue + données portfolio
    snap = {
        "timestamp":           now_str(),
        "spot":                round(spot, 2),
        "entry_spot":          p0["entry_spot"],
        "spot_move_pct":       round((spot / p0["entry_spot"] - 1) * 100, 3),
        "tte_days":            p0["tte_days"],
        "days_held":           days_held_max,
        # Option principale (pour compat dashboard)
        "entry_price_btc":     p0["entry_price_btc"],
        "current_price_btc":   p0["current_price_btc"],
        "current_ask_btc":     p0["current_ask_btc"],
        "current_bid_btc":     p0["current_bid_btc"],
        "current_iv_pct":      p0["current_iv_pct"],
        "entry_iv_pct":        p0["entry_iv_pct"],
        # PnL
        "pnl_option_usd":      round(pnl_option_total, 2),
        "pnl_hedge_usd":       round(pnl_hedge_usd, 2),
        "funding_pnl_usd":     round(funding_pnl_usd, 4),
        "total_pnl_usd":       round(total_pnl_usd, 2),
        "pnl_pct_of_premium":  p0["pnl_pct_of_premium"],
        # Greeks portfolio
        "live_delta":          round(net_delta, 5),
        "live_gamma":          round(net_gamma, 7),
        "live_vega":           round(net_vega,  4),
        "theta_daily_now_usd": round(net_theta, 2),
        "theta_theory_usd":    round(net_theta_theory, 2),
        "vrp_capture_pct":     round(vrp_portfolio, 1),
        # Hedge
        "hedge_qty":           hedge_qty,
        "hedge_delta_drift":   hedge_delta_drift,
        "hedge_threshold_pct": round(hedge_thr_pct, 2),
        "hedge_threshold_btc": round(hedge_thr_btc, 5),
        # Détail par position (pour dashboard multi)
        "positions_detail":    pos_snaps,
    }
    return snap


# ── compute_snapshot reste disponible pour compat greeks_hedge.py ────────────
def compute_snapshot(position: dict) -> dict:
    """Wrapper compat : snapshot single position (lit hedge depuis positions.json)."""
    state = load_state()
    hedge = state.get("hedge", {})
    spot  = fetch_option_ticker(position["instrument_name"]).get("underlying_price") or fetch_spot()
    pos_snap = compute_option_pnl(position, spot)

    hedge_qty      = float(hedge.get("qty", position.get("hedge_qty", 0.0)))
    hedge_avg      = float(hedge.get("avg_entry", position.get("hedge_avg_entry", spot)))
    realized_hedge = float(hedge.get("realized_pnl_usd", position.get("realized_hedge_pnl_usd", 0.0)))

    funding_8h    = fetch_perp_funding()
    perp_mark     = fetch_perp_mark()
    funding_daily = funding_8h * 3
    hedge_notional = abs(hedge_qty) * perp_mark
    funding_pnl_usd = funding_daily * pos_snap["days_held"] * hedge_notional

    pnl_hedge_usd = hedge_qty * (spot - hedge_avg) + realized_hedge
    total_pnl_usd = pos_snap["pnl_option_usd"] + pnl_hedge_usd + funding_pnl_usd

    iv_scale      = math.sqrt(max(pos_snap["current_iv_pct"], 20.0) / 70.0)
    hedge_thr_pct = max(2.0, min(8.0, 5.0 * iv_scale))

    snap = {**pos_snap,
        "timestamp":           now_str(),
        "spot":                round(spot, 2),
        "spot_move_pct":       round((spot / pos_snap["entry_spot"] - 1) * 100, 3),
        "pnl_hedge_usd":       round(pnl_hedge_usd, 2),
        "funding_pnl_usd":     round(funding_pnl_usd, 4),
        "total_pnl_usd":       round(total_pnl_usd, 2),
        "hedge_qty":           hedge_qty,
        "hedge_delta_drift":   round(-pos_snap["live_delta"] - abs(hedge_qty), 5),
        "hedge_threshold_pct": round(hedge_thr_pct, 2),
        "hedge_threshold_btc": round(hedge_thr_pct / 100, 5),
    }
    return snap


def load_position() -> dict | None:
    state = load_state()
    positions = state.get("positions", [])
    return positions[0] if positions else None


def run_once(plot: bool = False, report: bool = False):
    state = load_state()
    positions = state.get("positions", [])
    if not positions:
        print("Aucune position ouverte dans positions.json")
        return

    print(f"Calcul snapshot portfolio ({len(positions)} position(s))...")
    snap = compute_portfolio_snapshot(state)

    # Affichage console (position principale)
    primary_pos = max(positions, key=lambda p: pd.to_datetime(
        p["expiry_dt"], utc=True).timestamp())
    display_snapshot(snap, primary_pos)
    save_snapshot(snap)

    snapshots = load_snapshots()
    if plot:
        plot_pnl(snapshots, primary_pos)
    if report:
        print_report(snapshots, primary_pos)

    # Export CSV
    tag = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    pd.DataFrame([{k: v for k, v in snap.items() if k != "positions_detail"}]).to_csv(
        OUTPUT_DIR / f"pnl_snap_{tag}.csv", index=False)

    # pnl_history.json
    history_file = Path(__file__).parent / "pnl_history.json"
    history: list = []
    if history_file.exists():
        try:
            history = json.loads(history_file.read_text())
        except Exception:
            history = []

    # Delta net = sum des deltas options (position vendeur = -delta)
    net_delta_abs = abs(float(snap.get("live_delta", 0)))
    gamma_pts = float(snap.get("live_gamma", 0)) * float(snap.get("spot", 0)) * 0.01 * 100
    hist_point = {
        "ts":            snap["timestamp"],
        "spot":          snap["spot"],
        "tte_days":      snap["tte_days"],
        "delta_pct":     round(net_delta_abs * 100, 3),
        "net_delta_pct": round(float(snap.get("hedge_delta_drift", 0)) * 100, 3),
        "gamma_pts":     round(gamma_pts, 4),
        "iv_pct":        snap.get("current_iv_pct"),
        "pnl_option":    snap.get("pnl_option_usd"),
        "pnl_hedge":     snap.get("pnl_hedge_usd"),
        "pnl_total":     snap.get("total_pnl_usd"),
        "theta_daily":   snap.get("theta_daily_now_usd"),
        "n_positions":   len(positions),
    }
    history.append(hist_point)
    history = history[-500:]
    history_file.write_text(json.dumps(history, indent=2))
    print(f"  pnl_history.json : {len(history)} points")

    push_positions()


def watch_loop(interval_min: int = 5):
    print(f"Mode WATCH — refresh toutes les {interval_min} min  (Ctrl+C pour arreter)")
    iteration = 0
    while True:
        iteration += 1
        print(f"\n[Iteration {iteration}]")
        try:
            run_once(plot=(iteration % 6 == 0))   # plot toutes les 30 min
        except Exception as e:
            print(f"  [ERREUR] {e}")
        print(f"  Prochain refresh dans {interval_min} min...")
        time.sleep(interval_min * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PnL Monitor — Short Put VRP")
    parser.add_argument("--watch",  type=int, metavar="MIN", nargs="?", const=5,
                        help="Mode watch, refresh toutes les N min (default: 5)")
    parser.add_argument("--plot",   action="store_true", help="Genere les graphiques")
    parser.add_argument("--report", action="store_true", help="Rapport complet")
    args = parser.parse_args()

    if args.watch:
        watch_loop(args.watch)
    else:
        run_once(plot=args.plot, report=args.report)
