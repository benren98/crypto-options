"""
margin.py — Capital à immobiliser sur Deribit pour porter le book (short puts + short perp).

Deux régimes de marge (support.deribit.com, vérifié le 2026-09-23) :

• Marge STANDARD (défaut, S:SM / X:SM) — exacte : chaque position est margée SÉPARÉMENT
  puis tout est additionné, SANS netting (le short perp du hedge s'ajoute à la marge des
  puts alors qu'il réduit le risque).
    short put (inverse BTC) : IM = max(max(0.15 − OTM/Index, 0.10) + mark, MM)   [BTC / contrat]
                              MM = max(0.075, 0.075·mark) + mark
    perpétuel BTC (tier 1)  : IM = 1/L(N), L = 50× pour notre taille (≪ 5 % du max) → 2 %
                              MM = 2/3 · IM

• PORTFOLIO MARGIN (S:PM / X:PM, sélectionnable dans le compte) — ESTIMATION : le book est
  stressé en bloc (±14 % de spot en 4 paliers de chaque côté × vol hausse / inchangée / baisse),
  IM = pire perte + roll shock, MM = 0,8 × IM. Paramètres BTC lus via public/pme/get_params ;
  la forme exacte du choc de vol n'est pas publiée → hypothèse ci-dessous (à valider avec
  private/simulate_portfolio dès qu'une clé API est disponible). La table étendue et le
  delta shock sont négligeables à notre taille (dampener 200 k$, seuil 20 M$).

Rémunération du collatéral : la doc marge/collatéral ne mentionne aucun intérêt sur les soldes.
Le cash immobilisé ne rapporte que via (1) un actif de collatéral qui porte un rendement
(USYC / BUIDL : T-bills tokenisés, décote 2 % ; stETH 7,5 % ; USDe 5 %) ou (2) du BTC en
collatéral neutralisé par un short perp supplémentaire → encaisse le funding (cash-and-carry).
"""
import math

# ── Marge standard (formules de la doc) ──────────────────────────────────────
SM_PUT_BASE    = 0.15     # 0.15 − OTM
SM_PUT_FLOOR   = 0.10     # plancher
SM_PUT_MM      = 0.075
PERP_IM        = 0.02     # 1 / 50× (tier 1, petite taille)
PERP_MM_RATIO  = 2 / 3

# ── Portfolio margin BTC (public/pme/get_params, 2026-09-23) ──────────────────
PM_PRICE_RANGE      = 0.14
PM_BUCKETS          = 4
PM_VOL_UP           = 0.40
PM_VOL_DOWN         = 0.25
PM_MIN_VOL_UP       = 0.50   # plancher de vol pour le choc à la hausse
PM_SHORT_VEGA_POWER = 0.30   # échéances < 30 j
PM_LONG_VEGA_POWER  = 0.13
PM_MM_FACTOR        = 0.80
PM_MIN_ANNUAL_MOVE  = 0.005  # roll shock minimal (× |delta net par échéance| × index)

# ── Rémunération du collatéral (hypothèses, modifiables) ──────────────────────
TBILL_YIELD = 0.04   # USYC / BUIDL (T-bills tokenisés) — accès souvent réservé aux investisseurs qualifiés


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def put_usd(S, K, T, sigma):
    """Put (Black, r = 0) en USD par BTC."""
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0)
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / sq
    return K * _ncdf(-(d1 - sq)) - S * _ncdf(-d1)


# ── Standard ─────────────────────────────────────────────────────────────────
def sm_short_put(S, K, mark_btc, contracts):
    """(IM, MM) en BTC d'un short put inverse — formules exactes Deribit."""
    otm = max(S - K, 0.0) / S
    mm = max(SM_PUT_MM, SM_PUT_MM * mark_btc) + mark_btc
    im = max(max(SM_PUT_BASE - otm, SM_PUT_FLOOR) + mark_btc, mm)
    return im * contracts, mm * contracts


def standard_margin(lots, hedge_qty, S):
    """lots : [{strike, mark_btc, contracts}] ; hedge_qty en BTC (signe indifférent).
    Retourne IM / MM en BTC et en USD, avec le détail options vs perp."""
    im_o = mm_o = 0.0
    for l in lots:
        i, m = sm_short_put(S, l["strike"], l.get("mark_btc", 0.0), l["contracts"])
        im_o += i; mm_o += m
    im_p = abs(hedge_qty) * PERP_IM
    mm_p = im_p * PERP_MM_RATIO
    im, mm = im_o + im_p, mm_o + mm_p
    return {"im_btc": im, "mm_btc": mm, "im_usd": im * S, "mm_usd": mm * S,
            "options_im_usd": im_o * S, "perp_im_usd": im_p * S}


# ── Portfolio (estimation) ────────────────────────────────────────────────────
def _vol_shock(iv, dte, up):
    """Choc de vol absolu (en décimal). HYPOTHÈSE de forme : amplitude × (30/DTE)^puissance,
    appliquée à max(IV, plancher) à la hausse — structure et paramètres de Deribit, forme non publiée."""
    power = PM_SHORT_VEGA_POWER if dte < 30 else PM_LONG_VEGA_POWER
    scale = (30.0 / max(dte, 1.0)) ** power
    return (max(iv, PM_MIN_VOL_UP) * PM_VOL_UP * scale) if up else (iv * PM_VOL_DOWN * scale)


def portfolio_margin(lots, hedge_qty, S):
    """lots : [{strike, tte_days, iv (décimal), contracts, delta (put, facultatif)}] ;
    hedge_qty : BTC signé (négatif = short). Retourne IM / MM USD et le pire scénario."""
    if not lots and not hedge_qty:
        return {"im_usd": 0.0, "mm_usd": 0.0, "worst": None}
    moves = [PM_PRICE_RANGE * k / PM_BUCKETS for k in range(-PM_BUCKETS, PM_BUCKETS + 1)]
    base = [(l, put_usd(S, l["strike"], l["tte_days"] / 365, l["iv"])) for l in lots]
    worst, worst_sc = 0.0, None
    for vs in ("up", "same", "down"):
        for m in moves:
            S1 = S * (1 + m)
            pnl = hedge_qty * (S1 - S)
            for l, p0 in base:
                iv = l["iv"]
                if vs != "same":
                    iv = max(0.01, iv + (1 if vs == "up" else -1) * _vol_shock(iv, l["tte_days"], vs == "up"))
                pnl -= (put_usd(S1, l["strike"], l["tte_days"] / 365, iv) - p0) * l["contracts"]
            if pnl < worst:
                worst, worst_sc = pnl, {"move": round(m * 100, 1), "vol": vs, "pnl_usd": round(pnl, 2)}
    # Roll shock (minimum) : |delta net (options courtes + perp) par échéance| × index × 0,5 %
    by_exp = {}
    for l in lots:
        by_exp[round(l["tte_days"], 1)] = by_exp.get(round(l["tte_days"], 1), 0.0) - l.get("delta", 0.0) * l["contracts"]
    roll = PM_MIN_ANNUAL_MOVE * S * (sum(abs(v) for v in by_exp.values()) + abs(hedge_qty))
    im = -worst + roll
    return {"im_usd": im, "mm_usd": im * PM_MM_FACTOR, "worst": worst_sc, "roll_usd": roll}
