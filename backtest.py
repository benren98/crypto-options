"""
backtest.py — Backtest de la stratégie VRP short put delta-hedgée, rejouée comme le bot live.

Données réelles : prix index horaires (funding_history.jsonl), DVOL journalier et horaire,
funding horaire, surfaces de vol enregistrées (vol_surface.jsonl) quand elles couvrent la date.
Prix d'options : smile réel du jour rescalé au DVOL de l'heure, sinon modèle
DVOL × niveau ATM par maturité × skew fité (vol_model_fit.json) ; Black-Scholes.

Chaque heure (miroir de greeks_hedge.run_once, cadence RUN_EVERY_H) :
    règlement des échéances à 08:00 UTC → rolls (TTE ≤ 1 j et gamma > 6) → circuit breaker
    (moves 24 h / 72 h, DVOL 72 h ; allègement / fermeture par rachat à l'ask ou par le hedge)
    → entrées (calendrier d'échéances Deribit, grille de strikes, score v2, 2 passes si le book
    est vide, plafond d'entrées par jour) → hedge delta (bande, cadence, urgence) → funding.
Clôture à 00:00 UTC : marks, equity, marge. Frais Deribit sur chaque transaction ; vente au
bid (mark − demi-spread), rachats à l'ask (mark + demi-spread ; + BUYBACK_IV_PREMIUM pour ceux du
circuit breaker, calibré sur les rachats réels du live).
Paramètres miroir du live vérifiés par check_params_sync.py.

Usage : python backtest.py [--years 4] [--always-one] [--no-cb] [--no-pm]   (défaut = config de production)
"""
import sys, math, argparse
sys.path.insert(0, '.')
from datetime import datetime, timedelta, timezone
import numpy as np
from scipy.special import ndtr as _ndtr
from greeks_hedge import get, now_ms
import margin as mg

# ── Paramètres stratégie (miroir de greeks_hedge.py) ──────────────────────────
ENTRY_SCORE_MIN   = 0.45    # abaissé avec SKEW_NORM=0.60/IVHV_NORM=1.50 (échelle des scores plus basse)
MAX_PORTFOLIO_BTC = 5.0
GAMMA_PEN_START   = 5.0
GAMMA_SCORE_CAP   = 10.0
DVOL_MIN          = 35.0
YIELD_NORM        = 0.30
SKEW_NORM         = 0.60     # entre-deux (dé-sature partiellement vs 0.20)
IVHV_NORM         = 1.50     # entre-deux — normalisation s_iv_hv = clamp((bid_iv/HV−1)/IVHV_NORM, 0,1)
HV_W5             = 0.0      # pondération de l'HV de référence (5j/10j/30j) — miroir live
HV_W10            = 0.5
HV_W30            = 0.5
RANK_FLOOR        = 0.7      # plancher du multiplicateur de rang DVOL (sizing) — routine 2026-07-06 (opt=1.0, 0.7 prudent)
SIZE_CONVEXITY    = 1.5     # taille ∝ score^1.5 (miroir greeks_hedge.compute_sizing)
MIN_PREMIUM_USD   = 150.0   # plancher de prime au bid ($/BTC) — anti-poussière (BTC ; backtest Calmar 3.56→4.40)
# Poids du score (skew-pondéré, miroir greeks_hedge ; expérience 0.65/SKEW_NORM 0.60 annulée)
SCORE_W_IVHV      = 0.30
SCORE_W_YIELD     = 0.25
SCORE_W_SKEW      = 0.45
ENTRY_SCORE_REENTRY_BOOST = 0.05  # marge au-dessus du score d'entrée pour recharger un instrument tenu
DELTA_MIN_SPACING         = 0.12  # même échéance ET |delta − delta_tenu| < seuil → traité comme une ré-entrée
                                  # (aligné sur le live depuis que la ré-entrée compare les dates d'échéance)
SCAN_DELTA_MIN            = -0.30 # plafond d'exposition : pas plus proche de l'ATM que −0.30 (miroir live)
SCAN_TTE_MIN              = 1.0   # échéances scannées : TTE réel entre MIN et MAX jours (miroir live)
SCAN_TTE_MAX              = 14.0
MAX_ENTRIES_PER_DAY       = 0     # nouvelles positions max par jour UTC (0 = illimité, miroir live)
ROLL_TRIGGER              = 1.0   # roll si TTE ≤ ROLL_TRIGGER j ET gamma > GAMMA_ROLL_THRESHOLD (miroir live)
GAMMA_ROLL_THRESHOLD      = 6.0

# ── Paramètres modèle de pricing ───────────────────────────────────────────────
SKEW_SLOPE        = 0.013   # IV(K) = DVOL × (1 + 0.013 × OTM%) — calibré juin 2026 (~1.3%/pt OTM)
BA_HAIRCUT_VOLPTS = 0.7     # demi-spread en pts de vol : bid ≈ mark − 0.7, ask ≈ mark + 0.7 (médiane mesurée
                            # sur les vraies surfaces, juin-sept. 2026, DVOL 35-45 ; était 1.5)…
BA_HAIRCUT_DVOL_REF = 40.0  # …élargi proportionnellement au DVOL au-delà de 40 (stress : spreads plus larges)
BUYBACK_IV_PREMIUM = 5.0    # hypothèse : pts de vol ajoutés aux rachats du CIRCUIT BREAKER (vente en stress :
                            # ask au-dessus du smile). Calibré : 5 pts reproduisent au $ près les 29 rachats
                            # réels des 18 et 24/06/2026 (DVOL 43-46 ; en stress violent, sans doute plus)
EXPIRY_CALENDAR   = "deribit" # "deribit" : quotidiennes + vendredis listés (comme le live) · "fixed" : TTE_CHOICES
FUNDING_DAILY     = 0.0001  # repli : ~0.01%/jour PAYÉ par le short perp si le funding réel manque ce jour-là
USE_REAL_FUNDING  = True    # funding réel horaire (funding_history.jsonl) : un short perp l'ENCAISSE s'il est > 0
TTE_CHOICES       = [3, 7, 14, 21]       # échéances candidates si EXPIRY_CALENDAR = "fixed" (ancien modèle)
# Deltas candidats : plancher retiré (SCAN_DELTA_MAX=0) → on inclut les far-OTM petits deltas.
# Le plancher de prime écarte ensuite ceux trop bon marché. Filtrés par SCAN_DELTA_MIN.
DELTA_TARGETS     = [-0.05, -0.08, -0.12, -0.16, -0.20, -0.25, -0.30]
STRIKE_GRID       = 500.0   # strikes arrondis à la grille Deribit (rend « même instrument » possible)

# ── Frais Deribit — grille Standard vérifiée le 2026-09-23 (support.deribit.com, page Fees) ──
FEE_OPTION_RATE   = 0.0003   # options : 3 bps du sous-jacent par contrat (maker = taker)…
FEE_OPTION_CAP    = 0.125    # …plafonné à 12,5 % de la prime
FEE_DELIVERY_RATE = 0.00015  # livraison 1,5 bps, plafonnée à 12,5 % de la valeur à l'échéance → 0 si OTM
FEE_PERP_RATE     = 0.00035  # perpétuel taker 3,5 bps (maker 1,5) — rebalancements supposés taker
FEE_MULT          = 1.0      # multiplicateur de stress (routine : hypothèse testée, jamais « optimisée »)

# ── Hedge delta (miroir greeks_hedge.compute_hedge_order) ─────────────────────
HEDGE_THRESHOLD_BASE_PCT = 5.0        # bande = BASE × √(IV_ref/HEDGE_IV_REF), bornée [2 ; 8] %
HEDGE_IV_REF             = 70.0       # IV_ref = IV max des positions (comme le live)
HEDGE_THRESHOLD_MODE     = "absolute" # "absolute" : bande en BTC fixe (live) · "notional" : × Σ contrats
HEDGE_RATIO              = 1.0        # fraction du delta couverte — 1.0 depuis le 2026-09-24 (miroir live)
HEDGE_FLATTEN_DELTA      = 0.0        # si |delta options| < X BTC → hedge remis à plat (0 = off)
HEDGE_EVERY_H            = 4          # rebalance « normal » au plus toutes les N heures (4 = live depuis le 2026-09-24)
HEDGE_CADENCE_EXEMPT     = True       # après un changement du book (entrée, expiration, allègement, fermeture)
                                      # le premier contrôle ignore la cadence (rehedge immédiat si seuil dépassé)
HEDGE_URGENT_MULT        = 0.0        # cadence > 1 h : rehedge immédiat si dérive > MULT × bande (0 = off)
RUN_EVERY_H              = 1          # hypothèse : cadence effective du process live (CB + hedge). Le cron
                                      # GitHub « horaire » tourne en réalité toutes les ~3 h (médiane, sept. 2026)
HEDGE_INTRADAY           = True     # hedge rejoué heure par heure (prix index horaires de funding_history)

# ── Capital immobilisé (margin.py) ────────────────────────────────────────────
TRACK_PM = False   # portfolio margin (estimation, ~2 s de plus par run) ; la marge standard est toujours suivie


def option_fee(S, price_usd, contracts):
    """Frais d'une transaction option (entrée ou rachat), en $."""
    return FEE_MULT * contracts * min(FEE_OPTION_RATE * S, FEE_OPTION_CAP * price_usd)


def delivery_fee(S, K, contracts):
    """Frais de livraison à l'échéance (nul pour un put qui expire OTM)."""
    return FEE_MULT * contracts * min(FEE_DELIVERY_RATE * S, FEE_OPTION_CAP * max(K - S, 0.0))


def perp_fee(S, qty):
    return FEE_MULT * abs(qty) * S * FEE_PERP_RATE


def hedge_threshold_btc(iv_ref_pct, contracts):
    """Bande de rebalancement en BTC — même formule que greeks_hedge.compute_hedge_threshold."""
    pct = HEDGE_THRESHOLD_BASE_PCT * math.sqrt(max(iv_ref_pct, 20.0) / HEDGE_IV_REF)
    pct = max(2.0, min(8.0, pct))
    scale = max(contracts, 1.0) if HEDGE_THRESHOLD_MODE == "notional" else 1.0
    return pct / 100.0 * scale


_FUNDING_BY_DAY = None
_HOURLY_BY_DAY = None

def _load_hourly():
    """funding_history.jsonl → (taux de funding cumulé par jour, [(prix index, taux 1h, ts ms)] par jour)."""
    global _FUNDING_BY_DAY, _HOURLY_BY_DAY
    if _FUNDING_BY_DAY is not None:
        return
    _FUNDING_BY_DAY, _HOURLY_BY_DAY = {}, {}
    try:
        import json as _j
        rows = []
        with open("funding_history.jsonl", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    r = _j.loads(line)
                    rows.append((int(r["ts"]), float(r.get("interest_1h") or 0.0), float(r.get("index_price") or 0.0)))
        rows.sort()
        for ts, rate, px in rows:
            d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
            _FUNDING_BY_DAY[d] = _FUNDING_BY_DAY.get(d, 0.0) + rate
            if px > 0:
                _HOURLY_BY_DAY.setdefault(d, []).append((px, rate, ts))
    except Exception:
        pass


def funding_by_day():
    """Somme des taux de funding horaires (interest_1h) par jour UTC."""
    _load_hourly()
    return _FUNDING_BY_DAY


def hourly_by_day():
    """Prix index BTC horaires (et taux de funding de l'heure) par jour UTC."""
    _load_hourly()
    return _HOURLY_BY_DAY

# ── Surface de vol : réelle quand disponible, sinon modèle (skew fité par maturité ou linéaire) ──
# Le skew quadratique est lu depuis vol_model_fit.json (surface fittée par bucket de maturité,
# fit_vol_model.py) ; à défaut, skew linéaire 0.013 = comportement d'origine.
SKEW_A, SKEW_B = SKEW_SLOPE, 0.0    # repli linéaire ultime
SKEW_SURFACE   = None               # liste de buckets régime-aware {dte_lo,dte_hi,a0,a1,b0,b1,dvol_ref}
SKEW_POOLED    = None               # fit poolé (repli si pas de bucket pour la maturité)
try:
    import json as _json_fit
    _fit = _json_fit.load(open("vol_model_fit.json", encoding="utf-8"))
    SKEW_SURFACE = _fit.get("buckets")
    SKEW_POOLED  = _fit.get("pooled")
    _na = sum(1 for bk in (SKEW_SURFACE or []) if bk.get("regime_aware"))
    print(f"[backtest] surface skew fitée chargée ({_fit.get('n_snapshots')}j, "
          f"{len(SKEW_SURFACE or [])} buckets, {_na} régime-aware)")
except Exception:
    pass

USE_REAL_SURFACE = True   # utilise les vraies IV enregistrées pour les dates couvertes
try:
    import vol_surface_data as _vs
    _vs_cov = _vs.coverage()
    if _vs_cov:
        print(f"[backtest] surface réelle disponible : {_vs_cov['start']}→{_vs_cov['end']} "
              f"({_vs_cov['days']}j) — utilisée pour ces dates, modèle ailleurs")
except Exception:
    _vs = None


def _bucket(dte):
    """Bucket de maturité de la surface fitée (repli : fit poolé, puis None)."""
    if SKEW_SURFACE and dte is not None:
        for _bk in SKEW_SURFACE:
            if _bk["dte_lo"] <= dte < _bk["dte_hi"]:
                return _bk
    return SKEW_POOLED


def level_factor(dte=None, dvol=None) -> float:
    """Vol ATM de l'échéance / DVOL (structure par terme fitée, 1.0 sans fit). Le DVOL est une
    vol ATM à 30 jours : les échéances de 1-3 semaines cotent ~0,93× en régime calme. Sans ce
    facteur, le modèle surestimait la vol de ~3,5 pts → primes du backtest trop riches."""
    bk = _bucket(dte)
    if not bk or "l0" not in bk:
        return 1.0
    dc = (dvol - bk.get("l_ref", 0.0)) if dvol is not None else 0.0
    return max(0.5, bk["l0"] + bk.get("l1", 0.0) * dc)


def skew_factor(otm_pct: float, dte=None, dvol=None) -> float:
    """Multiplicateur de skew IV(K)/IV_ATM. Surface par maturité ET conditionnée au
    régime de vol si fitée : a(DVOL)=a0+a1·(DVOL−ref), idem b. Repli : bucket statique,
    puis fit poolé, puis linéaire."""
    o = otm_pct if otm_pct > 0 else 0.0
    bk = _bucket(dte)
    if bk is None:
        return 1.0 + SKEW_A * o + SKEW_B * o * o   # repli linéaire
    dc = (dvol - bk.get("dvol_ref", 0.0)) if dvol is not None else 0.0
    a = bk["a0"] + bk.get("a1", 0.0) * dc
    b = bk["b0"] + bk.get("b1", 0.0) * dc
    return 1.0 + a * o + b * o * o


def iv_pct(S, K, dvol, date=None, dte=None):
    """mark IV (%) : réelle si la date est couverte par le dataset enregistré,
    sinon modèle (DVOL × skew, surface par maturité conditionnée au régime). Bascule
    automatique → supprime le risque modèle sur la période enregistrée."""
    if USE_REAL_SURFACE and _vs is not None and date is not None and dte is not None:
        c = _vs.iv_curve(date, dte)
        if c is not None:
            sd = _vs.snap_dvol(date)   # smile du snapshot rescalé au DVOL du moment
            return float(np.interp(K / S, c[0], c[1])) * (dvol / sd if sd and dvol else 1.0)
    otm = (S - K) / S * 100
    return dvol * level_factor(dte, dvol) * skew_factor(otm, dte, dvol)

N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
n_pdf = lambda x: math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

def bs_put(S, K, T, sigma):
    """Prix put BS (en $), delta, gamma. T en années, sigma en décimal."""
    if T <= 0:
        return max(K - S, 0.0), -1.0 if S < K else 0.0, 0.0
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / sq
    d2 = d1 - sq
    price = K * N(-d2) - S * N(-d1)
    delta = N(d1) - 1.0
    gamma = n_pdf(d1) / (S * sq)
    return price, delta, gamma

def strike_for_delta(S, T, sigma_atm, target_delta):
    """Strike OTM donnant ~target_delta, skew inclus. Bissection (delta put décroît
    avec K : K↑ → plus ITM → delta plus négatif)."""
    lo, hi = S * 0.40, S * 1.0   # strikes OTM (delta ~0 à K bas, ~-0.5 à l'ATM)
    K = 0.5 * (lo + hi)
    for _ in range(60):
        K = 0.5 * (lo + hi)
        otm = max((S - K) / S * 100, 0.0)
        sig = (sigma_atm * level_factor(T * 365, sigma_atm * 100)
               * skew_factor(otm, T * 365, sigma_atm * 100))   # niveau + skew par maturité × régime
        _, d, _ = bs_put(S, K, T, sig)
        if abs(d - target_delta) < 0.0005:
            break
        if d < target_delta:   # trop négatif → strike trop haut → baisser hi
            hi = K
        else:                  # pas assez négatif → monter lo
            lo = K
    return K

def _bs_put_vec(S, K, T, sig):
    """bs_put vectorisé (numpy) : prix ($), delta, gamma."""
    sq = sig * np.sqrt(T)
    d1 = (np.log(S / K) + 0.5 * sig * sig * T) / sq
    d2 = d1 - sq
    return K * _ndtr(-d2) - S * _ndtr(-d1), _ndtr(d1) - 1.0, np.exp(-0.5 * d1 * d1) / 2.5066282746310002 / (S * sq)


def _skew_vec(otm, dte, dvol):
    """skew_factor vectorisé."""
    o = np.maximum(otm, 0.0)
    bk = _bucket(dte)
    if bk is None:
        return 1.0 + SKEW_A * o + SKEW_B * o * o
    dc = dvol - bk.get("dvol_ref", 0.0)
    a = bk["a0"] + bk.get("a1", 0.0) * dc
    b = bk["b0"] + bk.get("b1", 0.0) * dc
    return 1.0 + a * o + b * o * o


def ba_haircut(dvol):
    """Demi-spread (pts de vol) : BA_HAIRCUT_VOLPTS, élargi avec le DVOL au-delà de la référence."""
    return BA_HAIRCUT_VOLPTS * max(1.0, dvol / BA_HAIRCUT_DVOL_REF)


_EPOCH = datetime(1970, 1, 1).date()


def listed_expiries(d, now_t):
    """Échéances listées vues depuis l'instant now_t (jours depuis l'epoch), jour UTC d, en
    instants de règlement (jours depuis l'epoch) : Deribit règle à 08:00 UTC les quotidiennes
    (J..J+3) et les vendredis jusqu'à 5 semaines (mensuelles incluses). Calendrier « fixed » :
    TTE_CHOICES jours depuis maintenant (ancien modèle)."""
    if EXPIRY_CALENDAR == "fixed":
        return [now_t + t for t in TTE_CHOICES]
    fs = {d + timedelta(days=j) for j in (0, 1, 2, 3)}
    fs |= {d + timedelta(days=j) for j in range(1, 36) if (d + timedelta(days=j)).weekday() == 4}
    return sorted((f - _EPOCH).days + 8 / 24 for f in fs)


def scan_candidates(S, dvol, date, hv_blend, now_t):
    """Scan du live rejoué : tous les puts listés (échéances × grille de strikes) avec TTE réel
    dans [SCAN_TTE_MIN, SCAN_TTE_MAX], greeks au mark, prix au bid, score v2 et filtres live
    (delta max, plancher de prime, cap gamma). now_t = instant (jours depuis l'epoch).
    Candidats (score, K, instant de règlement, prix $, otm %, delta, mark_iv), meilleur d'abord."""
    Ks = np.arange(math.ceil(S * 0.5 / STRIKE_GRID) * STRIKE_GRID, S * 0.98 + 1e-9, STRIKE_GRID)
    if len(Ks) == 0 or not hv_blend:
        return []
    otm = (S - Ks) / S * 100
    hc = ba_haircut(dvol)
    real = USE_REAL_SURFACE and _vs is not None
    out_s, out_i = [], []
    for k in listed_expiries(date, now_t):   # k = instant de règlement
        Td = k - now_t
        if not (SCAN_TTE_MIN <= Td <= SCAN_TTE_MAX):
            continue
        T = Td / 365
        lf = level_factor(Td, dvol)
        atm = dvol * lf
        curve = _vs.iv_curve(date, Td) if real else None
        if curve is not None:   # smile réel du jour rescalé au DVOL de l'heure ; ATM interpolé au spot
            sd = _vs.snap_dvol(date)
            sc_ = dvol / sd if sd else 1.0
            mark = np.interp(Ks / S, curve[0], curve[1]) * sc_
            atm = float(np.interp(1.0, curve[0], curve[1])) * sc_
        else:
            mark = dvol * lf * _skew_vec(otm, Td, dvol)
        bid = mark - hc
        price = _bs_put_vec(S, Ks, T, bid / 100)[0]
        _, delta, gamma = _bs_put_vec(S, Ks, T, mark / 100)
        g_pts = gamma * S
        s_ivhv = np.minimum(np.maximum((bid / hv_blend - 1.0) / IVHV_NORM, 0.0), 1.0)
        z = (otm / 100) / max(hv_blend / 100 * math.sqrt(T), 1e-9)
        s_yield = np.minimum(1.0, (price / S) / T * z / YIELD_NORM)
        s_skew = np.minimum(np.maximum((bid / atm - 1.0) / SKEW_NORM, 0.0), 1.0)
        g_fac = np.minimum(np.maximum(1.0 - np.maximum(0.0, g_pts - GAMMA_PEN_START)
                                      / (GAMMA_SCORE_CAP - GAMMA_PEN_START), 0.0), 1.0)
        score = (SCORE_W_IVHV * s_ivhv + SCORE_W_YIELD * s_yield + SCORE_W_SKEW * s_skew) * g_fac
        ok = (delta >= SCAN_DELTA_MIN) & (price >= MIN_PREMIUM_USD)
        if GAMMA_ENTRY_CAP > 0:
            ok &= g_pts <= GAMMA_ENTRY_CAP
        idx = np.nonzero(ok)[0]
        if len(idx):
            out_s.append(score[idx])
            out_i.append((k, Ks[idx], price[idx], otm[idx], delta[idx], mark[idx]))
    if not out_s:
        return []
    return _ranked(out_s, out_i)


def _ranked(out_s, out_i):
    """Candidats par score décroissant, produits à la demande (le premier autorisé suffit)."""
    sc = np.concatenate(out_s)
    src = [(j, i) for j, a in enumerate(out_s) for i in range(len(a))]
    for n in np.argsort(-sc, kind="stable"):
        j, i = src[n]
        k, Ks, pr, ot, de, mk = out_i[j]
        yield (float(sc[n]), float(Ks[i]), k, float(pr[i]), float(ot[i]), float(de[i]), float(mk[i]))


# ── Données historiques ────────────────────────────────────────────────────────
_HIST_CACHE = {}


def fetch_dvol_hourly(years: float):
    """DVOL horaire {fin de bougie (ms) : clôture} sur `years` ans (API paginée, mémoïsé) :
    le live lit le DVOL de l'heure (porte d'entrée, rang, jambe DVOL du circuit breaker)."""
    key = ("dvol_h", round(years, 2))
    if key in _HIST_CACHE:
        return _HIST_CACHE[key]
    end_ts = now_ms()
    start_ts = end_ts - int(years * 365 * 24 * 3600 * 1000)
    rows, page_end = {}, end_ts
    for _ in range(80):
        d = get('get_volatility_index_data', {'currency': 'BTC', 'start_timestamp': start_ts,
                                              'end_timestamp': page_end, 'resolution': '3600'})
        for r in d.get('data', []):
            if r[4]:
                rows[int(r[0]) + 3_600_000] = float(r[4])
        cont = d.get('continuation')
        if not cont or cont <= start_ts or cont >= page_end:
            break
        page_end = cont
    _HIST_CACHE[key] = rows
    return rows   # mémoïse le fetch (la routine rejoue ~100 backtests → 1 seul fetch)

def fetch_history(years: float):
    _key = round(years, 2)
    if _key in _HIST_CACHE:
        return _HIST_CACHE[_key]
    end_ts   = now_ms()
    start_ts = end_ts - int(years * 365 * 24 * 3600 * 1000)
    spot_d = get('get_tradingview_chart_data', {
        'instrument_name': 'BTC-PERPETUAL',
        'start_timestamp': start_ts, 'end_timestamp': end_ts, 'resolution': '1D'})
    # L'API DVOL renvoie au plus 1000 points par appel + un jeton `continuation` (fin de la
    # page suivante) : sans pagination, le backtest « 4 ans » ne couvrait que ~2,7 ans.
    dvol_rows, page_end = [], end_ts
    for _ in range(20):
        dvol_d = get('get_volatility_index_data', {
            'currency': 'BTC', 'start_timestamp': start_ts, 'end_timestamp': page_end, 'resolution': '1D'})
        dvol_rows.extend(dvol_d.get('data', []))
        cont = dvol_d.get('continuation')
        if not cont or cont <= start_ts or cont >= page_end:
            break
        page_end = cont
    dvol_by_day = {datetime.fromtimestamp(r[0]/1000, tz=timezone.utc).date(): r[4] for r in dvol_rows}
    days = []
    for tick, close in zip(spot_d['ticks'], spot_d['close']):
        d = datetime.fromtimestamp(tick/1000, tz=timezone.utc).date()
        if d in dvol_by_day and close:
            days.append({'date': d, 'spot': close, 'dvol': dvol_by_day[d]})
    _HIST_CACHE[_key] = days
    return days

def hv_std(closes, n):
    """HV annualisée comme greeks_hedge.fetch_hv : écart-type centré (n−1) des n derniers log-returns."""
    if len(closes) < n + 1:
        return None
    w = closes[-(n+1):]
    rets = [math.log(w[i]/w[i-1]) for i in range(1, len(w))]
    m = sum(rets) / len(rets)
    return math.sqrt(sum((r - m) ** 2 for r in rets) / max(len(rets) - 1, 1)) * math.sqrt(365) * 100


def hv_from(closes, n):
    """HV « RMS » (sans centrage) — comme le hv_5d du circuit breaker live."""
    if len(closes) < n + 1:
        return None
    w = closes[-(n+1):]
    rets = [math.log(w[i]/w[i-1]) for i in range(1, len(w))]
    return math.sqrt(sum(r*r for r in rets)/len(rets)) * math.sqrt(365) * 100

# ── Backtest ───────────────────────────────────────────────────────────────────
def rank_mult_linear(iv_rank: float) -> float:
    """Multiplicateur de rang DVOL : monotone RANK_FLOOR -> 1.0."""
    return RANK_FLOOR + (1.0 - RANK_FLOOR) * iv_rank

def rank_mult_bell(iv_rank: float) -> float:
    """Profil en cloche : 0.5 en bas de range, pic 1.0 vers rank 0.65,
    réduit à 0.6 à l'extrême haut (crash en cours / imminent)."""
    if iv_rank <= 0.65:
        return 0.5 + 0.5 * (iv_rank / 0.65)
    return 1.0 - 0.4 * (iv_rank - 0.65) / 0.35

# ── Circuit breaker (aligné sur greeks_hedge.py live : 10% / +12pts, baisse seule) ─
CB_MOVE_3D_PCT   = 10.0   # palier dur : ferme tout si move spot 3j < −10% (baisse seule)
CB_DVOL_3D_PTS   = 100.0  # jambe DVOL désactivée (miroir live, 2026-09-24)
CB_REENTRY_MOVE  = 4.0    # re-entrée (depuis fermeture) : |move 3j| < 4% et HV5 < HV10
# Palier d'allègement gradué (miroir greeks_hedge : move1=5 OU move3=6 → trim à 30%, reprise si move3<3)
GRADUATED_CB     = False
CB_T1_MOVE_1D    = 5.0
CB_T1_MOVE_3D    = 6.0
CB_T1_KEEP       = 0.30
CB_T1_RESTORE    = 3.0
CB_T1_ACTION     = "buyback"  # allègement : "buyback" = rachat de (1−KEEP) des puts à l'ask (live) ·
                              # "hedge" = CANDIDAT sans code live : aucun rachat, hedge porté à
                              # CB_T1_HEDGE_RATIO du delta et entrées bloquées jusqu'à la reprise
CB_T1_HEDGE_RATIO = 1.0
CB_CLOSE_ACTION  = "buyback"  # palier dur : "buyback" = tout racheter à l'ask (live) · "hedge" = CANDIDAT :
                              # on garde les puts jusqu'à l'échéance, hedge à CB_T1_HEDGE_RATIO, entrées bloquées
CB_T1_COOLDOWN_D = 0      # jours sans redéclenchement T1 après une reprise (anti-whipsaw ; 0 = off)
# Le live évalue le CB à chaque run horaire (fenêtres glissantes 24 h / 72 h) : on le rejoue
# heure par heure quand les prix horaires existent. False = ancien contrôle à la clôture seule.
CB_INTRADAY      = True
GAMMA_ENTRY_CAP  = 0.0    # refuse l'entrée si gamma_pts > cap, même si score OK (0 = off)

def capital_stats(curve, cap_series):
    """Capital à déposer = pic de marge initiale + pire drawdown (les pertes réduisent l'equity :
    sans ce coussin, le compte passerait sous la marge au pire moment).
    Rendement annualisé sur ce capital selon la rémunération du collatéral :
      • idle        : USDC / BTC non couvert, aucun rendement (Deribit ne rémunère pas les soldes)
      • tbill       : T-bills tokenisés (USYC / BUIDL, décote 2 %) au taux margin.TBILL_YIELD
      • btc_funding : collatéral en BTC neutralisé par un short perp → encaisse le funding réel"""
    if not cap_series or not curve:
        return None
    peak_eq, dd = curve[0][1], 0.0
    for c in curve:
        peak_eq = max(peak_eq, c[1]); dd = max(dd, peak_eq - c[1])
    K = max(cap_series) + dd
    if K <= 0:
        return None
    years = len(curve) / 365
    pnl = curve[-1][1]
    fund = funding_by_day()
    carry = {"idle": 0.0,
             "tbill": K * mg.TBILL_YIELD * years,
             "btc_funding": sum(K * fund.get(c[0], 0.0) for c in curve)}
    return {"capital_usd": round(K), "peak_margin_usd": round(max(cap_series)), "buffer_dd_usd": round(dd),
            "avg_margin_usd": round(sum(cap_series) / len(cap_series)),
            "roc_pct": {k: round((pnl + v) / years / K * 100, 1) for k, v in carry.items()},
            "carry_usd": {k: round(v) for k, v in carry.items()}}


def run(years: float, always_one: bool = False, rank_mult=rank_mult_linear,
        circuit_breaker: bool = False, label: str = "", verbose: bool = False):
    days = fetch_history(years + 0.15)   # marge pour warmup HV30
    fund_day = funding_by_day() if USE_REAL_FUNDING else {}
    hourly = hourly_by_day() if HEDGE_INTRADAY else {}
    hour_idx = 0            # compteur d'heures (cadence de rebalancement)
    last_rebal_h = -10**9
    closes_hist = []
    positions = []      # {strike, exp_t (règlement, jours depuis l'epoch), contracts, entry_premium_usd…}
    hedge_qty = 0.0     # BTC short (positif = short)
    hedge_vwap = 0.0
    cash = 0.0          # PnL cumulé réalisé ($)
    equity_curve = []
    n_trades = n_expired_itm = n_rebal = 0
    fees = {"options": 0.0, "delivery": 0.0, "perp": 0.0}
    funding_total = 0.0
    attrib = {"options": 0.0, "hedge": 0.0}   # PnL réalisé hors frais/funding (options clôturées, hedge)
    cap_sm, cap_pm = [], []                   # marge initiale jour par jour ($)
    worst_days = []
    notionals = []
    notionals_usd = []
    dvol_30 = []
    dvol_hist = []
    risk_off = False
    cb_reduced = False
    n_cb_triggers = 0
    n_t1_trims = 0
    n_cb_intraday = 0   # actions du CB (fermeture ou allègement) prises en cours de journée
    cb_days_off = 0
    t1_block_until_h = -1   # heure (hour_idx) avant laquelle un nouveau trim T1 est interdit (cooldown)
    hourly_px = []      # [(ts ms, prix)] sur ~80 h glissantes (fenêtres 24 h / 72 h du CB)
    cb_events = []      # [(date, "close"|"trim", "intraday"|"close")] — journal des actions du CB
    day_pnl = 0.0
    now_t = 0.0         # instant courant (jours depuis l'epoch) : TTE = règlement − maintenant
    dvol_hourly = fetch_dvol_hourly(years + 0.15) if HEDGE_INTRADAY else {}
    entries_today = 0   # nouvelles positions ouvertes ce jour UTC (MAX_ENTRIES_PER_DAY)
    n_rolls = 0
    entry_log = []      # [(date, heure, K, TTE j, delta, taille, score, spot)] — comparaison avec le live

    def rebalance(target, S):
        """Amène le short perp à `target` (BTC, positif = short) : PnL réalisé, VWAP, frais."""
        nonlocal cash, day_pnl, hedge_qty, hedge_vwap, n_rebal
        dq = target - hedge_qty
        if abs(dq) < 1e-12:
            return
        if hedge_qty != 0 and (dq * hedge_qty < 0):   # réduction → réalise PnL
            closed = min(abs(dq), abs(hedge_qty)) * (1 if hedge_qty > 0 else -1)
            cash += closed * (hedge_vwap - S)          # short : gain si S < vwap
            day_pnl += closed * (hedge_vwap - S)
            attrib["hedge"] += closed * (hedge_vwap - S)
        if target != 0:
            if hedge_qty * target > 0 and abs(target) > abs(hedge_qty):
                hedge_vwap = (hedge_vwap * abs(hedge_qty) + S * abs(dq)) / abs(target)
            elif hedge_qty * target <= 0:
                hedge_vwap = S
        else:
            hedge_vwap = 0.0
        f = perp_fee(S, dq)
        fees["perp"] += f; cash -= f; day_pnl -= f
        hedge_qty = target
        n_rebal += 1

    book_changed = False   # le book a changé depuis le dernier contrôle de hedge

    def hedge_step(net_delta, S, ivs, contracts, force_cadence=False):
        """Politique de hedge (miroir live) : cible = −delta × ratio, mise à plat du résiduel,
        bande IV-dépendante, cadence minimale entre deux rebalancements (sauf juste après un
        changement du book si HEDGE_CADENCE_EXEMPT)."""
        nonlocal last_rebal_h, book_changed
        ratio = (CB_T1_HEDGE_RATIO if (cb_reduced and CB_T1_ACTION == "hedge")
                 or (risk_off and CB_CLOSE_ACTION == "hedge") else HEDGE_RATIO)
        target = -net_delta * ratio
        flatten = HEDGE_FLATTEN_DELTA > 0 and abs(net_delta) < HEDGE_FLATTEN_DELTA
        if flatten:
            target = 0.0
        thr = hedge_threshold_btc(max(ivs) if ivs else HEDGE_IV_REF, contracts)
        due = abs(target - hedge_qty) > thr or (flatten and abs(hedge_qty) > 1e-9)
        exempt = HEDGE_CADENCE_EXEMPT and book_changed
        book_changed = False
        urgent = HEDGE_URGENT_MULT > 0 and abs(target - hedge_qty) > HEDGE_URGENT_MULT * thr
        if due and (force_cadence or exempt or urgent or hour_idx - last_rebal_h >= HEDGE_EVERY_H):
            rebalance(target, S)
            last_rebal_h = hour_idx

    def rem_days(p):
        """Temps restant (jours) jusqu'au règlement de 08:00 UTC, à l'instant courant."""
        return p['exp_t'] - now_t

    def iv_h(p, dvol_now):
        """IV d'une position à l'heure courante : IV du dernier mark rescalée au DVOL de l'heure."""
        return p.get('iv_now', dvol_now) * dvol_now / (p.get('iv_dvol') or dvol_now)

    def buy_back(p, n_close, S, dvol, date, stress=True):
        """Rachète n_close contrats d'une position à l'ask (mark + demi-spread) ; rachats du circuit
        breaker (stress=True) : + BUYBACK_IV_PREMIUM (vente en stress). Renvoie le PnL réalisé."""
        Td = max(rem_days(p), 0.01)
        extra = ba_haircut(dvol) + (BUYBACK_IV_PREMIUM if stress else 0.0)
        sig = (iv_pct(S, p['strike'], dvol, date, Td) + extra) / 100
        price, _, _ = bs_put(S, p['strike'], Td / 365, sig)
        f = option_fee(S, price, n_close)
        fees["options"] += f
        gross = p['entry_premium_usd'] * (n_close / p['contracts']) - price * n_close
        attrib["options"] += gross
        return gross - f

    def settle(p, S_set):
        """Règlement à l'échéance (08:00 UTC) : payoff au prix du moment + frais de livraison."""
        nonlocal cash, day_pnl, n_expired_itm
        payoff = max(p['strike'] - S_set, 0.0) * p['contracts']
        f = delivery_fee(S_set, p['strike'], p['contracts'])
        fees["delivery"] += f
        attrib["options"] += p['entry_premium_usd'] - payoff
        cash += p['entry_premium_usd'] - payoff - f
        day_pnl += p['entry_premium_usd'] - payoff - f
        if payoff > 0:
            n_expired_itm += 1

    def cb_step(S, m1, m3, dvol_chg, hv5, hv10, dvol, date, daily):
        """Machine d'état du circuit breaker (miroir apply_circuit_breaker du live).
        m1/m3 = moves signés 1 j / 3 j (%), dvol_chg = variation du DVOL sur 3 j (pts).
        Renvoie True si le book a changé (fermeture ou allègement)."""
        nonlocal positions, cash, day_pnl, risk_off, cb_reduced, n_cb_triggers, n_t1_trims
        nonlocal t1_block_until_h, book_changed
        if not risk_off and positions and (m3 < -CB_MOVE_3D_PCT or dvol_chg > CB_DVOL_3D_PTS):
            # Palier dur : tout racheter à l'ask (on paie le spread + les frais en sortie) — ou,
            # variante « hedge », garder les puts, hedger à CB_T1_HEDGE_RATIO et bloquer les entrées
            if CB_CLOSE_ACTION == "buyback":
                for p in positions:
                    r = buy_back(p, p['contracts'], S, dvol, date)
                    cash += r; day_pnl += r
                positions = []
                rebalance(0.0, S)
            book_changed = True
            risk_off = True
            cb_reduced = False
            n_cb_triggers += 1
            cb_events.append((str(date), "close", "close" if daily else "intraday"))
            return True
        if GRADUATED_CB and not risk_off and not cb_reduced and positions and hour_idx >= t1_block_until_h and \
                (m1 < -CB_T1_MOVE_1D or m3 < -CB_T1_MOVE_3D):
            # Palier d'allègement : rachat de (1−keep) de chaque position à l'ask — ou, variante
            # « hedge », aucun rachat : le hedge monte à CB_T1_HEDGE_RATIO au prochain contrôle
            for p in (positions if CB_T1_ACTION == "buyback" else []):
                sell = p['contracts'] * (1.0 - CB_T1_KEEP)
                r = buy_back(p, sell, S, dvol, date)
                cash += r; day_pnl += r
                p['entry_premium_usd'] *= CB_T1_KEEP
                p['contracts'] *= CB_T1_KEEP
            positions = [p for p in positions if p['contracts'] > 1e-9]
            cb_reduced = True
            book_changed = True
            n_t1_trims += 1
            cb_events.append((str(date), "trim", "close" if daily else "intraday"))
            return True
        if cb_reduced and abs(m3) < CB_T1_RESTORE:
            cb_reduced = False
            t1_block_until_h = hour_idx + int(CB_T1_COOLDOWN_D * 24)   # anti-whipsaw : N × 24 h (comme le live)
        elif risk_off:
            # Re-entrée : réalisé court se retourne + spot stabilisé
            if hv5 is not None and hv10 is not None and hv5 < hv10 and abs(m3) < CB_REENTRY_MOVE:
                risk_off = False
        return False

    def try_entries(S_e, dvol_e, hv_e, rank_e, date_e, passes):
        """Entrées d'un run (miroir greeks_hedge.run_once) : jusqu'à `passes` ouvertures (le live
        enchaîne le bloc « book vide » et le bloc opportuniste), bloquées en risk-off, pendant
        l'allègement du CB, sous DVOL_MIN, au cap notionnel et au plafond d'entrées du jour."""
        nonlocal cash, day_pnl, n_trades, book_changed, entries_today
        for _ in range(passes):
            used = sum(p['contracts'] for p in positions)
            must_open = always_one and not positions and not risk_off
            if risk_off or cb_reduced:
                return
            if not ((dvol_e >= DVOL_MIN and used < MAX_PORTFOLIO_BTC) or must_open):
                return
            if MAX_ENTRIES_PER_DAY and entries_today >= MAX_ENTRIES_PER_DAY:
                return
            # Positions tenues par instrument (échéance, strike) : score moyen pondéré ; espacement
            # mesuré au delta D'ENTRÉE (miroir greeks_hedge.held_info)
            held = {}
            for p in positions:
                h = held.setdefault((round(p['exp_t'], 4), p['strike']),
                                    {'exp': round(p['exp_t'], 4), 'delta': p.get('delta_entry', p['delta_now']),
                                     'sc': 0.0, 'n': 0.0})
                h['sc'] += p.get('score_entry', ENTRY_SCORE_MIN) * p['contracts']
                h['n']  += p['contracts']
            for h in held.values():
                h['score'] = h['sc'] / h['n'] if h['n'] else ENTRY_SCORE_MIN

            def _allowed(K_c, exp_c, delta_c, score_c):
                """Filtre ré-entrée (miroir greeks_hedge._candidate_allowed)."""
                exp_c = round(exp_c, 4)
                if (exp_c, K_c) in held:
                    return score_c > held[(exp_c, K_c)]['score'] + ENTRY_SCORE_REENTRY_BOOST
                close = [h for h in held.values()
                         if h['exp'] == exp_c and abs(delta_c - h['delta']) < DELTA_MIN_SPACING]
                if close:
                    wavg = sum(h['score'] * h['n'] for h in close) / sum(h['n'] for h in close)
                    return score_c > wavg + ENTRY_SCORE_REENTRY_BOOST
                return True

            best = next((c for c in scan_candidates(S_e, dvol_e, date_e, hv_e, now_t)
                         if _allowed(c[1], c[2], c[5], c[0])), None)
            if not best or not (best[0] >= ENTRY_SCORE_MIN or must_open):
                return
            score, K, exp_t, price, otm, delta, mark_iv = best
            size = round(score ** SIZE_CONVEXITY * rank_mult(rank_e), 1)
            size = round(min(max(0.1, size), MAX_PORTFOLIO_BTC - used), 1)
            if size < 0.1:
                return
            f = option_fee(S_e, price, size)
            fees["options"] += f
            cash -= f; day_pnl -= f
            positions.append({
                'strike': K, 'exp_t': exp_t, 'contracts': size, 'entry_premium_usd': price * size,
                'score_entry': score, 'delta_entry': delta, 'delta_now': delta,
                'iv_now': mark_iv, 'iv_dvol': dvol_e, 'mark_usd': price,
            })
            n_trades += 1
            entries_today += 1
            book_changed = True
            entry_log.append((str(date_e), round((now_t % 1) * 24), K, round(exp_t - now_t, 2), round(delta, 3),
                              size, round(score, 3), round(S_e)))

    def _px_ago(ts, hours):
        """Prix horaire le plus proche de ts − hours (tolérance 6 h, comme le live), sinon None."""
        target = ts - hours * 3_600_000
        i = len(hourly_px) - 1 - hours           # série horaire régulière : accès direct
        if 0 <= i < len(hourly_px) and abs(hourly_px[i][0] - target) < 1_800_000:
            return hourly_px[i][1]
        best = min(hourly_px, key=lambda r: abs(r[0] - target), default=None)
        return best[1] if best and abs(best[0] - target) < 6 * 3_600_000 else None

    def _dvol_ago(ts, hours):
        """DVOL horaire à ts − hours (ou l'heure voisine)."""
        t = ts - hours * 3_600_000
        return dvol_hourly.get(t) or dvol_hourly.get(t - 3_600_000) or dvol_hourly.get(t + 3_600_000)

    for day in days:
        # Clôture journalière à 00:00 UTC (comme le DVOL journalier et la boucle horaire) : la bougie
        # 1D du perp se clôt à 08:00 UTC le lendemain — son prix aurait 8 h d'avance sur le reste.
        nxt = hourly.get(day['date'] + timedelta(days=1)) if hourly else None
        S = nxt[0][0] if nxt else day['spot']
        dvol = day['dvol']
        closes_hist.append(S)
        dvol_30.append(dvol)
        dvol_30 = dvol_30[-30:]
        dvol_hist.append(dvol)
        hv10, hv30 = hv_std(closes_hist, 10), hv_std(closes_hist, 30)
        if hv10 is None or hv30 is None or len(dvol_30) < 10:
            continue
        entries_today = 0
        hv5 = hv_from(closes_hist, 5)            # RMS, comme le hv_5d du circuit breaker live
        hv5s = hv_std(closes_hist, 5)
        hv_blend = HV_W5 * (hv5s if hv5s else hv10) + HV_W10 * hv10 + HV_W30 * hv30
        iv_rank  = max(0.0, min(1.0, (dvol - min(dvol_30)) / max(max(dvol_30) - min(dvol_30), 5)))
        dvol_chg_3d = dvol - dvol_hist[-4] if len(dvol_hist) >= 4 else 0.0
        t_close = (day['date'] - _EPOCH).days + 1.0

        day_pnl = 0.0

        # ── Runs horaires (cadence du live) : règlements 08:00 → rolls → CB → entrées → hedge.
        # Seules les infos connues à l'heure h : prix et DVOL de l'heure, clôtures de la veille.
        hours = hourly.get(day['date']) if HEDGE_INTRADAY else None
        if hours:
            hv5_y  = hv_from(closes_hist[:-1], 5)
            hv10_y = hv_std(closes_hist[:-1], 10)
            dvol_y = dvol_hist[-2] if len(dvol_hist) >= 2 else dvol
            d30_y = dvol_30[:-1] or dvol_30
            for px, h_rate, ts in hours:
                hour_idx += 1
                hourly_px.append((ts, px))
                del hourly_px[:-80]
                now_t = ts / 86_400_000
                dvol_h = dvol_hourly.get(ts) or dvol_y
                # Règlement des échéances (l'exchange règle à 08:00, que le process tourne ou non)
                if positions and any(p['exp_t'] <= now_t + 1e-9 for p in positions):
                    for p in positions:
                        if p['exp_t'] <= now_t + 1e-9:
                            settle(p, px)
                    positions = [p for p in positions if p['exp_t'] > now_t + 1e-9]
                    book_changed = True
                runs_now = hour_idx % max(int(RUN_EVERY_H), 1) == 0   # le process tourne-t-il à cette heure ?
                if runs_now and positions:
                    keep = []
                    for p in positions:
                        rem = rem_days(p)
                        if rem <= ROLL_TRIGGER:
                            g = bs_put(px, p['strike'], max(rem, 0.01) / 365, iv_h(p, dvol_h) / 100)[2]
                            if g * px > GAMMA_ROLL_THRESHOLD:   # gamma en pts de delta pour 1 % de move
                                r = buy_back(p, p['contracts'], px, dvol_h, day['date'], stress=False)
                                cash += r; day_pnl += r
                                n_rolls += 1
                                book_changed = True
                                continue
                        keep.append(p)
                    positions = keep
                if runs_now and circuit_breaker and CB_INTRADAY:
                    p1, p3 = _px_ago(ts, 24), _px_ago(ts, 72)
                    m1 = (px / p1 - 1) * 100 if p1 else 0.0
                    m3 = (px / p3 - 1) * 100 if p3 else 0.0
                    dv3 = _dvol_ago(ts, 72)
                    if cb_step(px, m1, m3, (dvol_h - dv3) if dv3 else 0.0, hv5_y, hv10_y, dvol_h,
                               day['date'], daily=False):
                        n_cb_intraday += 1
                if runs_now:
                    hv_h = [hv_std(closes_hist[:-1] + [px], w) for w in (5, 10, 30)]
                    if hv_h[1] and hv_h[2]:
                        hv_e = HV_W5 * (hv_h[0] or hv_h[1]) + HV_W10 * hv_h[1] + HV_W30 * hv_h[2]
                        d30 = d30_y + [dvol_h]
                        rank_h = max(0.0, min(1.0, (dvol_h - min(d30)) / max(max(d30) - min(d30), 5)))
                        try_entries(px, dvol_h, hv_e, rank_h, day['date'], passes=2 if not positions else 1)
                    ivs = [iv_h(p, dvol_h) for p in positions]
                    nd = sum(bs_put(px, p['strike'], max(rem_days(p), 0.01) / 365, iv / 100)[1] * p['contracts']
                             for p, iv in zip(positions, ivs))
                    hedge_step(nd, px, ivs, sum(p['contracts'] for p in positions))
                if hedge_qty:
                    # Funding horaire : réel, ou forfait payé par le short (hypothèse « forfait »)
                    f_h = hedge_qty * px * h_rate if USE_REAL_FUNDING else -abs(hedge_qty) * px * FUNDING_DAILY / 24
                    funding_total += f_h; cash += f_h; day_pnl += f_h
        else:
            hour_idx += 24

        # ── Clôture (00:00 UTC du lendemain) ─────────────────────────────────
        now_t = t_close
        # Sans runs horaires (ou CB horaire coupé) : contrôle du CB et règlements à la clôture
        if circuit_breaker and not (hours and CB_INTRADAY):
            move_3d_signed = (S / closes_hist[-4] - 1) * 100 if len(closes_hist) >= 4 else 0.0
            move_1d_signed = (S / closes_hist[-2] - 1) * 100 if len(closes_hist) >= 2 else 0.0
            cb_step(S, move_1d_signed, move_3d_signed, dvol_chg_3d, hv5, hv10, dvol,
                    day['date'] + timedelta(days=1), daily=True)
        if risk_off:
            cb_days_off += 1
        if any(p['exp_t'] <= now_t + 1e-9 for p in positions):
            for p in positions:
                if p['exp_t'] <= now_t + 1e-9:
                    settle(p, S)
            positions = [p for p in positions if p['exp_t'] > now_t + 1e-9]
            book_changed = True

        # ── Mark-to-model + delta net (surface réelle du lendemain : le snapshot « D+1 » est pris
        # juste après la clôture de D)
        net_delta = 0.0
        mtm_value = 0.0     # valeur de rachat des puts vendus ($, négatif pour nous)
        pos_ivs = []
        for p in positions:
            rem = max(rem_days(p), 0.01)
            iv_m = iv_pct(S, p['strike'], dvol, day['date'] + timedelta(days=1), rem)
            price, delta, gamma = bs_put(S, p['strike'], rem / 365, iv_m / 100)
            p['delta_now'] = delta
            p['iv_now'] = iv_m
            p['iv_dvol'] = dvol
            p['mark_usd'] = price
            pos_ivs.append(iv_m)
            net_delta += delta * p['contracts']
            mtm_value += price * p['contracts']

        # ── Hedge et entrées à la clôture : seulement sans données horaires
        if not hours:
            hedge_step(net_delta, S, pos_ivs, sum(p['contracts'] for p in positions), force_cadence=True)
            rate = fund_day.get(day['date'])
            f_pnl = hedge_qty * S * rate if rate is not None else -abs(hedge_qty) * S * FUNDING_DAILY
            funding_total += f_pnl
            cash += f_pnl
            day_pnl += f_pnl
            try_entries(S, dvol, hv_blend, iv_rank, day['date'], passes=1)

        # ── Equity = cash + prime des positions ouvertes − valeur de rachat
        open_prem = sum(p['entry_premium_usd'] for p in positions)
        hedge_mtm = hedge_qty * (hedge_vwap - S)   # short flottant
        equity = cash + open_prem - mtm_value + hedge_mtm
        eq_prev = equity_curve[-1][1] if equity_curve else equity
        equity_curve.append((day['date'], equity, equity - eq_prev, S, dvol))
        worst_days.append((equity - eq_prev, day['date'], S, dvol))
        notional_track = sum(p['contracts'] for p in positions)
        notionals.append(notional_track)
        notionals_usd.append(notional_track * S)   # notionnel $ jour par jour

        # Capital immobilisé : marge standard exacte (+ portfolio margin estimée si TRACK_PM)
        sm = mg.standard_margin([{'strike': p['strike'], 'mark_btc': p.get('mark_usd', 0.0) / S,
                                  'contracts': p['contracts']} for p in positions], hedge_qty, S)
        cap_sm.append(sm['im_usd'])
        if TRACK_PM:
            pm = mg.portfolio_margin([{'strike': p['strike'], 'tte_days': max(rem_days(p), 0.05),
                                       'iv': p.get('iv_now', dvol) / 100, 'contracts': p['contracts'],
                                       'delta': p.get('delta_now', 0.0)} for p in positions],
                                     -hedge_qty, S)   # convention margin.py : négatif = short
            cap_pm.append(pm['im_usd'])

    # Expose pour analyse capital (rendement sur capital mobilisé)
    globals()['_LAST_RUN'] = {"curve": equity_curve, "notionals_usd": notionals_usd,
                              "fees": {k: round(v, 2) for k, v in fees.items()},
                              "funding": round(funding_total, 2), "rebalances": n_rebal, "trades": n_trades,
                              "attrib": {k: round(v, 2) for k, v in attrib.items()},
                              "rolls": n_rolls, "entries": entry_log,
                              "cb": {"closes": n_cb_triggers, "trims": n_t1_trims,
                                     "intraday": n_cb_intraday, "days_off": cb_days_off,
                                     "events": cb_events},
                              "capital": {"sm": capital_stats(equity_curve, cap_sm),
                                          "pm": capital_stats(equity_curve, cap_pm) if cap_pm else None}}

    # ── Stats ──────────────────────────────────────────────────────────────────
    eq = [e[1] for e in equity_curve]
    rets = [eq[i] - eq[i-1] for i in range(1, len(eq))]
    peak, max_dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        max_dd = max(max_dd, peak - v)
    mean_d = sum(rets)/len(rets)
    std_d  = (sum((r-mean_d)**2 for r in rets)/len(rets)) ** 0.5
    sharpe = mean_d / std_d * math.sqrt(365) if std_d > 0 else 0
    worst_days.sort()

    print(f"\n{'='*70}")
    print(f"  BACKTEST {equity_curve[0][0]} -> {equity_curve[-1][0]}  ({len(equity_curve)} jours)")
    print(f"  Regle >=1 position : {'ON' if always_one else 'OFF'}")
    print(f"{'='*70}")
    print(f"  PnL final        : {eq[-1]:>12,.0f} $")
    print(f"  PnL annualise    : {eq[-1]/len(eq)*365:>12,.0f} $/an  (sur notionnel max 5 BTC)")
    print(f"  Max drawdown     : {max_dd:>12,.0f} $")
    print(f"  Sharpe (daily)   : {sharpe:>12.2f}")
    print(f"  Trades           : {n_trades}  |  expires ITM : {n_expired_itm}")
    print(f"  Frais Deribit    : {sum(fees.values()):>12,.0f} $  (options {fees['options']:,.0f} · "
          f"livraison {fees['delivery']:,.0f} · perp {fees['perp']:,.0f} · {n_rebal} rebalancements)")
    print(f"  Funding hedge    : {funding_total:>+12,.0f} $  ({'réel' if USE_REAL_FUNDING else 'forfait'})")
    _net_real = attrib['options'] + attrib['hedge'] + funding_total - sum(fees.values())
    print(f"  Attribution      : options {attrib['options']:+,.0f} $ · hedge réalisé {attrib['hedge']:+,.0f} $"
          f"  →  prime conservée {(_net_real / attrib['options'] * 100) if attrib['options'] > 0 else 0:.0f} %")
    for lab, cs in (("standard", cap_sm), ("portfolio (est.)", cap_pm)):
        c = capital_stats(equity_curve, cs)
        if c:
            r = c["roc_pct"]
            print(f"  Capital {lab:<17}: {c['capital_usd']:>7,} $ (pic de marge {c['peak_margin_usd']:,} + DD "
                  f"{c['buffer_dd_usd']:,}) · marge moyenne {c['avg_margin_usd']:,} $  →  rendement/an "
                  f"{r['idle']:.1f} % (cash dormant) · {r['tbill']:.1f} % (T-bills) · {r['btc_funding']:.1f} % (BTC + funding)")
    if circuit_breaker:
        print(f"  Rolls            : {n_rolls}")
        print(f"  Circuit breaker  : {n_cb_triggers} fermetures · {n_t1_trims} allègements "
              f"({n_cb_intraday} en cours de journée)  |  {cb_days_off} jours risk-off")
    avg_not = sum(notionals)/len(notionals)
    avg_spot = sum(e[3] for e in equity_curve)/len(equity_curve)
    print(f"  Notionnel moyen  : {avg_not:.1f} BTC (~{avg_not*avg_spot:,.0f} $)  ->  rendement ~{eq[-1]/len(eq)*365/(avg_not*avg_spot)*100:.1f}%/an du notionnel")
    print(f"\n  10 pires jours :")
    for pnl, d, s, dv in worst_days[:10]:
        print(f"    {d}  {pnl:>10,.0f} $   spot {s:>10,.0f}  DVOL {dv:.0f}%")
    print(f"\n  Equity annuelle :")
    by_year = {}
    for d, e, *_ in equity_curve:
        by_year[d.year] = e
    prev = 0
    for y in sorted(by_year):
        print(f"    {y} : {by_year[y]-prev:>+12,.0f} $")
        prev = by_year[y]
    return equity_curve

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--years', type=float, default=4.0)
    ap.add_argument('--always-one', action='store_true', help='force la regle toujours >=1 position (off en prod)')
    ap.add_argument('--no-cb', action='store_true', help='desactive le circuit breaker (on en prod)')
    ap.add_argument('--no-pm', action='store_true', help='ne pas estimer la portfolio margin (plus rapide)')
    a = ap.parse_args()
    TRACK_PM = not a.no_pm
    # Par défaut = configuration de production (ALWAYS_IN_POSITION=False, circuit breaker actif)
    run(a.years, always_one=a.always_one, circuit_breaker=not a.no_cb)
