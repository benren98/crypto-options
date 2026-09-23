"""
generate_dashboard.py — Dashboard v2 (docs/v2.html), orienté décision.

Architecture : Python construit un modèle de données (dict → JSON) qui répond aux
questions qu'on se pose en ouvrant le dashboard ; le template HTML (dashboard_v2.html)
le rend côté navigateur (vanilla JS + Chart.js). Données et présentation séparées
(la v1 generate_html.py mélange les deux dans ~1 600 lignes de f-strings).

  1. Le bot tourne-t-il ?          → meta.bot_age_h, verdict
  2. Dois-je intervenir ?          → verdict (action / surveiller / ok)
  3. Pourquoi il (n')entre (pas) ? → gates + facteur limitant du meilleur candidat
  4. Quel risque je porte ?        → distance aux circuit breakers + stress test
  5. La stratégie gagne-t-elle ?   → PnL réalisé (options vs hedge vs funding), live vs backtest
  6. Que dit la routine ?          → décisions en attente (changements robustes)

Sources locales uniquement (aucun appel réseau) : positions.json, scan_entry.json,
positions_detail.json, pnl_history.json, funding_history.jsonl, backtest_routine.json,
reconcile.json (optionnel). Paramètres live lus dans greeks_hedge.py (source unique).

Usage :
    python generate_dashboard.py                        # → docs/v2.html
    python generate_dashboard.py --data-dir <dossier>   # rendre un autre état (tests)
"""
import argparse
import json
import math
import sys
from bisect import bisect_right
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from check_params_sync import parse_constants  # noqa: E402

TEMPLATE = HERE / "dashboard_v2.html"
REPO_URL = "https://github.com/benren98/crypto-options"

BOT_STALE_WARN_H   = 4.0   # cron horaire mais GitHub retarde souvent (p90 observé ≈ 4.5 h)
BOT_STALE_ACTION_H = 8.0
MARKET_DAYS        = 120   # profondeur des séries marché embarquées
JOURNAL_DAYS       = 60


# ── Utilitaires ────────────────────────────────────────────────────────────────

def parse_ts(v):
    """Parse les formats de timestamp du projet → datetime UTC (ou None)."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc)
    s = str(v).strip().replace(" UTC", "").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s.replace("+00:00", "+0000"), fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def fnum(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_put(S, K, T, sigma, r):
    """Prix BS d'un put européen (USD par BTC)."""
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0)
    sq = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sq)
    d2 = d1 - sigma * sq
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


# ── Construction du modèle ─────────────────────────────────────────────────────

class Model:
    def __init__(self, data_dir: Path, now: datetime):
        self.dir = data_dir
        self.now = now
        self.P = parse_constants(HERE / "greeks_hedge.py")
        self.state   = load_json(data_dir / "positions.json", {})
        self.scan    = load_json(data_dir / "scan_entry.json", {})
        self.details = load_json(data_dir / "positions_detail.json", [])
        self.history = load_json(data_dir / "pnl_history.json", [])
        self.routine = load_json(data_dir / "backtest_routine.json", {})
        self.recon   = load_json(data_dir / "reconcile.json", None)
        self.funding = self._load_funding(data_dir / "funding_history.jsonl")

        self.mc   = self.scan.get("market_context", {}) or {}
        self.spot = fnum(self.mc.get("spot")) or fnum((self.history[-1] if self.history else {}).get("spot"))
        self.positions = self.state.get("positions", []) or []
        self.closed    = self.state.get("history", []) or []
        self.hedge     = self.state.get("hedge", {}) or {}

    def p(self, name, default=None):
        v = self.P.get(name, default)
        return v if isinstance(v, (int, float, bool)) else default

    @staticmethod
    def _load_funding(path):
        rows = []
        if not path.exists():
            return rows
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    rows.append((int(r["ts"]), fnum(r.get("interest_1h")), fnum(r.get("index_price"))))
                except Exception:
                    continue
        rows.sort()
        return rows

    # ── Positions : appariement lot ↔ détail live (un-à-un) ────────────────────
    def _live_by_lot(self):
        pool = defaultdict(list)
        for d in self.details:
            pool[d.get("instrument")].append(d)
        out = {}
        for p in self.positions:
            avail = pool.get(p.get("instrument_name"), [])
            if not avail:
                continue
            ep, es = fnum(p.get("entry_price")), fnum(p.get("entry_spot"))
            best = min(avail, key=lambda d: abs(fnum(d.get("entry_price_btc")) - ep)
                       + abs(fnum(d.get("entry_spot")) - es) / 1e5)
            out[id(p)] = best
            avail.remove(best)
        return out

    def book(self):
        live = self._live_by_lot()
        groups = OrderedDict()
        for p in self.positions:
            groups.setdefault(p.get("instrument_name", "?"), []).append(p)

        rows = []
        tot = defaultdict(float)
        for name, lots in groups.items():
            p0 = lots[0]
            strike = fnum(p0.get("strike"))
            expiry = parse_ts(p0.get("expiry_dt"))
            tte = max(0.0, (expiry - self.now).total_seconds() / 86400) if expiry else 0.0
            n = sum(fnum(l.get("contracts"), 1) for l in lots)
            prem = sum(fnum(l.get("entry_price")) * fnum(l.get("entry_spot")) * fnum(l.get("contracts"), 1) for l in lots)
            pnl = delta = gamma = theta = 0.0
            iv_now, mark, marked = None, None, 0
            lot_rows = []
            for l in lots:
                c = fnum(l.get("contracts"), 1)
                d = live.get(id(l))
                if d:
                    marked += 1
                    pnl   += fnum(d.get("pnl_option_usd"))
                    delta += fnum(d.get("live_delta"))
                    gamma += fnum(d.get("live_gamma"))
                    theta += fnum(d.get("theta_daily_now_usd"))
                    iv_now = fnum(d.get("current_iv_pct")) or iv_now
                    mark   = fnum(d.get("current_price_btc"), None) if d.get("current_price_btc") is not None else mark
                else:
                    delta += fnum(l.get("delta_at_entry")) * c
                    gamma += fnum(l.get("gamma_at_entry")) * c
                lot_rows.append({
                    "entry_ts": iso(parse_ts(l.get("entry_ts"))),
                    "contracts": round(c, 4),
                    "score": l.get("entry_score"),
                    "entry_price": fnum(l.get("entry_price")),
                    "premium_usd": round(fnum(l.get("entry_price")) * fnum(l.get("entry_spot")) * c, 2),
                    "entry_spot": fnum(l.get("entry_spot")),
                    "iv_entry": fnum(l.get("iv_at_entry"), None),
                    "pnl_usd": round(fnum(d.get("pnl_option_usd")), 2) if d else None,
                })
            iv_pairs = [(fnum(l.get("iv_at_entry")), fnum(l.get("contracts"), 1)) for l in lots if fnum(l.get("iv_at_entry")) > 0]
            iv_entry = sum(v * c for v, c in iv_pairs) / sum(c for _, c in iv_pairs) if iv_pairs else None
            sc_pairs = [(fnum(l.get("entry_score")), fnum(l.get("contracts"), 1)) for l in lots if l.get("entry_score") is not None]
            score = sum(v * c for v, c in sc_pairs) / sum(c for _, c in sc_pairs) if sc_pairs else None
            gamma_pts = abs(gamma) / n * self.spot * 0.01 * 100 if n else 0.0
            rows.append({
                "instrument": name, "strike": strike, "expiry": iso(expiry), "tte_days": round(tte, 2),
                "dist_pct": round((strike / self.spot - 1) * 100, 2) if self.spot else None,
                "contracts": round(n, 4), "premium_usd": round(prem, 2), "pnl_usd": round(pnl, 2),
                "captured_pct": round(pnl / prem * 100, 1) if prem else None,
                "delta_btc": round(delta, 4), "gamma_pts": round(gamma_pts, 2), "theta_usd": round(theta, 2),
                "iv_entry": round(iv_entry, 1) if iv_entry else None, "iv_now": round(iv_now, 1) if iv_now else None,
                "mark_btc": mark, "score": round(score, 3) if score is not None else None,
                "lots": lot_rows, "marked": marked == len(lots),
                "roll_watch": tte <= fnum(self.p("ROLL_TRIGGER", 1.0)) and gamma_pts > fnum(self.p("GAMMA_ROLL_THRESHOLD", 6.0)),
                "_sigma": (iv_now or iv_entry or 50.0) / 100, "_tte_y": tte / 365,
            })
            tot["contracts"] += n; tot["premium"] += prem; tot["pnl"] += pnl
            tot["delta"] += delta; tot["theta"] += theta
        rows.sort(key=lambda r: r["tte_days"])

        cap = fnum(self.p("MAX_PORTFOLIO_BTC", 5.0))
        eff_cap = cap * (fnum(self.p("CB_T1_KEEP", 0.3)) if self.state.get("cb_reduced") else 1.0)
        hqty = fnum(self.hedge.get("qty"))
        # Convention projet : live_delta = Σ delta put × n (négatif) ; short put = −live_delta ;
        # hedge short (qty < 0) → résidu non couvert = −Σdelta + qty
        drift = -tot["delta"] + hqty
        iv_ref = max([r["iv_now"] or r["iv_entry"] or 0 for r in rows], default=0) or fnum(self.p("HEDGE_IV_REF", 70.0))
        thr_pct = max(2.0, min(8.0, fnum(self.p("HEDGE_THRESHOLD_BASE_PCT", 5.0))
                               * math.sqrt(max(iv_ref, 20.0) / fnum(self.p("HEDGE_IV_REF", 70.0)))))
        return rows, {
            "contracts": round(tot["contracts"], 4), "cap": cap, "eff_cap": eff_cap,
            "premium_usd": round(tot["premium"], 2), "latent_usd": round(tot["pnl"], 2),
            "theta_usd_day": round(tot["theta"], 2), "options_delta_btc": round(-tot["delta"], 4),
            "hedge_qty": round(hqty, 5), "hedge_avg": fnum(self.hedge.get("avg_entry"), None),
            "drift_btc": round(drift, 5), "threshold_btc": round(thr_pct / 100, 5), "threshold_pct": round(thr_pct, 2),
            "min_tte": min((r["tte_days"] for r in rows), default=None),
        }

    # ── Stress test (hypothèse sticky-strike, BS) ──────────────────────────────
    def stress(self, rows):
        if not rows or not self.spot:
            return None
        r = fnum(self.p("RISK_FREE_RATE", 0.05))
        hqty = fnum(self.hedge.get("qty"))
        shocks = [-15, -10, -7, -5, -3, 3, 5, 10]
        out = []
        for dv, label in ((0.0, "vol inchangée"), (0.10, "vol +10 pts")):
            line = []
            for s in shocks:
                S1 = self.spot * (1 + s / 100)
                d_opt = 0.0
                for g in rows:
                    v0 = bs_put(self.spot, g["strike"], g["_tte_y"], g["_sigma"], r)
                    v1 = bs_put(S1, g["strike"], g["_tte_y"], g["_sigma"] + dv, r)
                    d_opt -= (v1 - v0) * g["contracts"]
                d_hedge = hqty * (S1 - self.spot)
                line.append({"shock": s, "options": round(d_opt), "hedge": round(d_hedge), "total": round(d_opt + d_hedge)})
            out.append({"label": label, "points": line})
        return {"shocks": shocks, "scenarios": out}

    # ── Circuit breaker : distance aux déclencheurs ────────────────────────────
    def circuit_breaker(self):
        state = "risk_off" if self.state.get("risk_off") else ("reduced" if self.state.get("cb_reduced") else "armed")
        t1_1d = fnum(self.p("CB_T1_MOVE_1D_PCT", 5.0)); t1_3d = fnum(self.p("CB_T1_MOVE_3D_PCT", 6.0))
        t2_3d = fnum(self.p("CB_MOVE_3D_PCT", 10.0));   t2_dv = fnum(self.p("CB_DVOL_3D_PTS", 12.0))

        def meter(label, value, scale, ticks, adverse_sign, unit, hint):
            if value is None:
                return {"label": label, "value": None, "fill": 0, "ticks": ticks, "unit": unit, "hint": hint, "level": "na"}
            adverse = max(0.0, value * adverse_sign)
            fill = min(1.0, adverse / scale)
            first = min(t for t, _ in ticks)
            level = "critical" if adverse >= first else ("warning" if adverse >= 0.6 * first else "ok")
            return {"label": label, "value": round(value, 2), "fill": round(fill, 3),
                    "ticks": [{"at": round(t / scale, 3), "label": lbl} for t, lbl in ticks],
                    "unit": unit, "hint": hint, "level": level}

        mv1, mv3, dv3 = self.mc.get("cb_move_1d"), self.mc.get("cb_move_3d"), self.mc.get("cb_dvol_3d")
        meters = [
            meter("Spot 24 h", fnum(mv1, None) if mv1 is not None else None, t1_1d * 1.25,
                  [(t1_1d, f"allègement −{t1_1d:g}%")], -1, "%", "seule la baisse compte"),
            meter("Spot 3 jours", fnum(mv3, None) if mv3 is not None else None, t2_3d * 1.15,
                  [(t1_3d, f"allègement −{t1_3d:g}%"), (t2_3d, f"fermeture −{t2_3d:g}%")], -1, "%",
                  "seule la baisse compte"),
            meter("DVOL 3 jours", fnum(dv3, None) if dv3 is not None else None, t2_dv * 1.25,
                  [(t2_dv, f"fermeture +{t2_dv:g} pts")], 1, " pts", "hausse de la vol implicite"),
        ]
        info = self.state.get("cb_reduced_info") if state == "reduced" else self.state.get("risk_off_info")
        last = next((h for h in sorted(self.closed, key=lambda h: str(h.get("exit_ts", "")), reverse=True)
                     if h.get("exit_reason") in ("cb_tier1_trim", "circuit_breaker")), None)
        return {"state": state, "meters": meters, "since": iso(parse_ts((info or {}).get("ts"))),
                "restore_rule": (f"|move 3j| < {fnum(self.p('CB_T1_RESTORE_MOVE_PCT', 3.0)):g}%" if state == "reduced"
                                 else f"HV5 < HV10 et |move 3j| < {fnum(self.p('CB_REENTRY_MOVE_PCT', 4.0)):g}%" if state == "risk_off"
                                 else None),
                "last_event": {"ts": iso(parse_ts(last.get("exit_ts"))), "type": last.get("exit_reason")} if last else None}

    # ── Scanner & portes d'entrée ──────────────────────────────────────────────
    def scanner(self):
        w = {"vrp": fnum(self.p("SCORE_W_IVHV", 0.30)), "yield": fnum(self.p("SCORE_W_YIELD", 0.25)),
             "skew": fnum(self.p("SCORE_W_SKEW", 0.45))}
        rows = []
        for c in self.scan.get("top7", []) or []:
            g = fnum(c.get("gamma_factor"), 1.0)
            rows.append({
                "instrument": c.get("instrument_name"), "strike": fnum(c.get("strike")),
                "tte_days": fnum(c.get("tte_days")), "delta": fnum(c.get("delta")),
                "dist_pct": fnum(c.get("moneyness")), "premium_usd": fnum(c.get("premium_usd")),
                "bid_iv": fnum(c.get("bid_iv")), "iv_hv": fnum(c.get("iv_hv_ratio")),
                "skew_pct": fnum(c.get("skew_pct")), "yield_pct": fnum(c.get("yield_ann_pct")),
                "ba_pct": fnum(c.get("ba_pct")), "gamma_pts": fnum(c.get("gamma_pts")),
                "score": fnum(c.get("score")), "gamma_factor": g,
                "contrib": {"vrp": round(w["vrp"] * fnum(c.get("s_iv_hv")) * g, 4),
                            "yield": round(w["yield"] * fnum(c.get("s_yield")) * g, 4),
                            "skew": round(w["skew"] * fnum(c.get("s_skew")) * g, 4)},
                "status": c.get("status", "eligible"),
                "held_score": fnum(c.get("held_entry_score"), None) if c.get("held_entry_score") is not None else None,
            })
        return rows, w

    def gates(self, book_tot, scan_rows, weights):
        thr = fnum(self.p("ENTRY_SCORE_MIN", 0.45))
        dvol_min = fnum(self.p("DVOL_MIN", 35.0))
        dvol = fnum(self.mc.get("curr_iv"), None)
        free = book_tot["eff_cap"] - book_tot["contracts"]
        eligible = [r for r in scan_rows if r["status"] in ("eligible", "held_reentry")]
        best = max(eligible, key=lambda r: r["score"], default=None)
        gates = [
            {"key": "risk_off", "label": "Circuit breaker total inactif", "ok": not self.state.get("risk_off"),
             "detail": "risk-off : book fermé" if self.state.get("risk_off") else "OK"},
            {"key": "cb_reduced", "label": "Pas d'allègement CB en cours", "ok": not self.state.get("cb_reduced"),
             "detail": "allègement actif — entrées gelées" if self.state.get("cb_reduced") else "OK"},
            {"key": "dvol", "label": f"DVOL ≥ {dvol_min:g}", "ok": bool(self.mc.get("signal_ok")),
             "detail": f"DVOL {dvol:.1f}" if dvol is not None else "n/a"},
            {"key": "capacity", "label": "Capacité disponible", "ok": free >= 0.1,
             "detail": f"{book_tot['contracts']:.2f} / {book_tot['eff_cap']:.1f} BTC"},
            {"key": "score", "label": f"Meilleur candidat ≥ {thr:.2f}", "ok": bool(best and best["score"] >= thr),
             "detail": f"{best['score']:.3f} ({best['instrument']})" if best else "aucun candidat éligible"},
        ]
        limiting = None
        if best:
            names = {"vrp": "VRP (IV/HV)", "yield": "yield ajusté au risque", "skew": "skew vs ATM"}
            gf = best["gamma_factor"] or 1.0
            norm = {k: (best["contrib"][k] / (weights[k] * gf) if weights[k] else 0.0) for k in weights}
            k = min(norm, key=norm.get)   # composante la plus faible (sur 1)
            detail = {"vrp": (f"IV au bid / HV = {best['iv_hv']:.2f}× : la vol réalisée a rattrapé l'implicite, "
                              "il reste très peu de prime de risque à vendre") if best["iv_hv"] < 1.2
                             else f"IV au bid / HV = {best['iv_hv']:.2f}×",
                      "yield": f"yield {best['yield_pct']:.0f} %/an à {abs(best['dist_pct']):.1f} % du spot",
                      "skew": f"skew {best['skew_pct']:+.1f} % vs ATM : smile plat, peu de prime de crash"}[k]
            limiting = {"component": names[k], "detail": detail, "gap": round(thr - best["score"], 3),
                        "norm": {kk: round(v, 2) for kk, v in norm.items()}}
        blocked = next((g for g in gates if not g["ok"]), None)
        why = None
        if blocked:
            why = {"risk_off": "circuit breaker total actif",
                   "cb_reduced": "allègement CB en cours",
                   "dvol": f"DVOL {dvol:.1f} sous la porte de {dvol_min:g}" if dvol is not None else "DVOL indisponible",
                   "capacity": f"capacité pleine ({book_tot['contracts']:.2f} / {book_tot['eff_cap']:.1f} BTC)",
                   "score": (f"meilleur candidat {best['score']:.3f} < seuil {thr:.2f}" if best
                             else "aucun candidat éligible")}[blocked["key"]]
        return {"open": all(g["ok"] for g in gates), "gates": gates, "threshold": thr,
                "best": best, "limiting": limiting, "why": why,
                "blocked_key": blocked["key"] if blocked else None}

    # ── Performance réalisée ───────────────────────────────────────────────────
    def performance(self, book_tot):
        start = min((parse_ts(h.get("entry_ts")) for h in self.closed + self.positions if parse_ts(h.get("entry_ts"))),
                    default=None)
        opt_events = sorted((parse_ts(h.get("exit_ts")), fnum(h.get("pnl_usd"))) for h in self.closed if parse_ts(h.get("exit_ts")))

        hedge_events, cum = [], 0.0
        for e in sorted(self.hedge.get("history", []) or [], key=lambda e: str(e.get("ts", ""))):
            t = parse_ts(e.get("ts"))
            if not t:
                continue
            if e.get("realized_cumul_usd") is not None:
                new = fnum(e.get("realized_cumul_usd"))
                hedge_events.append((t, new - cum)); cum = new
            else:
                hedge_events.append((t, fnum(e.get("realized_pnl_usd"))))
                cum += fnum(e.get("realized_pnl_usd"))
        hedge_real = fnum(self.hedge.get("realized_pnl_usd"), cum)
        if abs(hedge_real - cum) > 0.5 and hedge_events:   # réconcilie avec le total officiel
            hedge_events.append((hedge_events[-1][0], hedge_real - cum))

        funding_events = self._funding_events()
        funding_total = sum(v for _, v in funding_events)
        fee_events, fee_split = self._fee_events()
        fees_total = sum(v for _, v in fee_events)          # négatif

        opt_real = sum(v for _, v in opt_events)
        hqty, havg = fnum(self.hedge.get("qty")), fnum(self.hedge.get("avg_entry"))
        hedge_mtm = hqty * (self.spot - havg) if hqty else 0.0
        latent = book_tot["latent_usd"]
        net = opt_real + hedge_real + hedge_mtm + funding_total + latent + fees_total

        # Séries journalières cumulées (réalisé, net de frais estimés)
        daily = []
        if start:
            day, end = start.date(), self.now.date()
            keys = ("options", "hedge", "funding", "fees")
            ev = {"options": opt_events, "hedge": hedge_events, "funding": funding_events, "fees": fee_events}
            idx = {k: 0 for k in keys}
            acc = {k: 0.0 for k in keys}
            while day <= end:
                lim = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
                for k in keys:
                    while idx[k] < len(ev[k]) and ev[k][idx[k]][0] < lim:
                        acc[k] += ev[k][idx[k]][1]; idx[k] += 1
                daily.append({"d": day.isoformat(), **{k: round(acc[k], 2) for k in keys},
                              "net": round(sum(acc.values()), 2)})
                day += timedelta(days=1)

        monthly = defaultdict(lambda: {"options": 0.0, "hedge": 0.0, "funding": 0.0, "fees": 0.0})
        for key, events in (("options", opt_events), ("hedge", hedge_events),
                            ("funding", funding_events), ("fees", fee_events)):
            for t, v in events:
                monthly[t.strftime("%Y-%m")][key] += v
        months = [{"m": m, **{k: round(v, 2) for k, v in d.items()},
                   "net": round(sum(d.values()), 2)} for m, d in sorted(monthly.items())]

        # Statistiques par événement (lots d'un même instrument clôturés la même minute)
        events = OrderedDict()
        for h in self.closed:
            key = (h.get("instrument_name"), h.get("exit_reason"), str(h.get("exit_ts", ""))[:16])
            events.setdefault(key, 0.0)
            events[key] += fnum(h.get("pnl_usd"))
        by_reason = defaultdict(lambda: {"n": 0, "pnl": 0.0})
        for (_, reason, _), v in events.items():
            by_reason[reason or "?"]["n"] += 1
            by_reason[reason or "?"]["pnl"] += v
        wins = sum(1 for v in events.values() if v > 0)

        days_live = max(1.0, (self.now - start).total_seconds() / 86400) if start else None
        bt = self.routine.get("baseline", {}) or {}
        bt_years = fnum(self.routine.get("years"), 4.0)
        live_annual = net / days_live * 365 if days_live else None
        bt_annual = fnum(bt.get("pnl")) / bt_years if bt.get("pnl") else None
        return {
            "start": iso(start), "days": round(days_live, 1) if days_live else None,
            "totals": {"options_realized": round(opt_real, 2), "hedge_realized": round(hedge_real, 2),
                       "hedge_mtm": round(hedge_mtm, 2), "funding": round(funding_total, 2),
                       "latent_options": round(latent, 2), "fees": round(fees_total, 2),
                       "fees_split": {k: round(v, 2) for k, v in fee_split.items()},
                       "net_before_fees": round(net - fees_total, 2), "net": round(net, 2)},
            "kept_pct": round(net / opt_real * 100, 1) if opt_real > 0 else None,
            "daily": daily, "monthly": months,
            "events": {"n": len(events), "wins": wins, "lots": len(self.closed),
                       "by_reason": {k: {"n": v["n"], "pnl": round(v["pnl"], 2)} for k, v in by_reason.items()}},
            "vs_backtest": {"live_annual": round(live_annual) if live_annual is not None else None,
                            "bt_annual": round(bt_annual) if bt_annual else None,
                            "bt_calmar": bt.get("calmar"), "bt_maxdd": bt.get("maxdd")},
        }

    def _fee_events(self):
        """Frais Deribit ESTIMÉS (le bot est en paper, rien n'est réellement débité) avec la grille
        de greeks_hedge.py (FEE_*), identique à celle du backtest :
        entrée de chaque lot, rachat (allègement CB, fermeture CB, roll), livraison si ITM,
        et chaque rebalancement du hedge perp. Retourne ([(date, −frais)], répartition)."""
        rate, cap = fnum(self.p("FEE_OPTION_RATE", 0.0003)), fnum(self.p("FEE_OPTION_CAP", 0.125))
        drate, prate = fnum(self.p("FEE_DELIVERY_RATE", 0.00015)), fnum(self.p("FEE_PERP_RATE", 0.00035))
        out, split = [], {"options": 0.0, "livraison": 0.0, "perp": 0.0}

        def add(ts, amount, key):
            t = parse_ts(ts)
            if t and amount > 0:
                out.append((t, -amount)); split[key] -= amount

        for lot in self.closed + self.positions:
            n, es, ep = fnum(lot.get("contracts"), 1), fnum(lot.get("entry_spot")), fnum(lot.get("entry_price"))
            add(lot.get("entry_ts"), n * min(rate * es, cap * ep * es), "options")
        for lot in self.closed:
            n, xs, xp = fnum(lot.get("contracts"), 1), fnum(lot.get("exit_spot")), fnum(lot.get("exit_price"))
            reason = lot.get("exit_reason")
            if reason in ("cb_tier1_trim", "circuit_breaker", "roll"):
                add(lot.get("exit_ts"), n * min(rate * xs, cap * xp * xs), "options")
            elif reason in ("expiration", "expired", "expiry"):
                intrinsic = max(fnum(lot.get("strike")) - xs, 0.0)
                add(lot.get("exit_ts"), n * min(drate * xs, cap * intrinsic), "livraison")
        for e in self.hedge.get("history", []) or []:
            add(e.get("ts"), abs(fnum(e.get("qty"))) * fnum(e.get("spot")) * prate, "perp")
        out.sort(key=lambda x: x[0])
        return out, split

    def _funding_events(self):
        """Funding réellement encaissé/payé par le hedge : Σ −qty(t) × index × interest_1h."""
        evs = sorted(((parse_ts(e.get("ts")), fnum(e.get("qty_after"))) for e in self.hedge.get("history", []) or []
                      if parse_ts(e.get("ts"))), key=lambda x: x[0])
        if not evs or not self.funding:
            return []
        ts_list = [int(t.timestamp() * 1000) for t, _ in evs]
        qtys = [q for _, q in evs]
        out = []
        for ts, rate, px in self.funding:
            if ts < ts_list[0]:
                continue
            q = qtys[bisect_right(ts_list, ts) - 1]
            if q:
                out.append((datetime.fromtimestamp(ts / 1000, tz=timezone.utc), -q * px * rate))
        return out

    # ── Séries marché ─────────────────────────────────────────────────────────
    def market_series(self, rows):
        cutoff = self.now - timedelta(days=MARKET_DAYS)
        cut_ms = int(cutoff.timestamp() * 1000)
        spot, fund = [], []
        window = []
        for ts, rate, px in self.funding:
            if ts < cut_ms - 7 * 86400 * 1000:
                continue
            window.append((ts, rate))
            while window and window[0][0] < ts - 7 * 86400 * 1000:
                window.pop(0)
            if ts >= cut_ms and (ts // 3_600_000) % 4 == 0:   # un point toutes les 4 h
                t = iso(datetime.fromtimestamp(ts / 1000, tz=timezone.utc))
                spot.append({"t": t, "v": round(px, 1)})
                fund.append({"t": t, "v": round(sum(r for _, r in window) / len(window) * 24 * 365 * 100, 2)})
        if self.spot and (not spot or spot[-1]["t"] < iso(self.now - timedelta(hours=2))):
            spot.append({"t": iso(parse_ts(self.scan.get("ts")) or self.now), "v": round(self.spot, 1)})
        vol = []
        for p in self.history:
            t = parse_ts(p.get("ts"))
            if not t or t < cutoff:
                continue
            h10, h30 = p.get("hv_10d"), p.get("hv_30d")
            vol.append({"t": iso(t), "dvol": p.get("dvol"),
                        "hv": round((fnum(h10) + fnum(h30)) / 2, 2) if h10 is not None and h30 is not None else None})
        strikes = sorted({r["strike"] for r in rows})
        return {"spot": spot, "funding": fund, "vol": vol, "strikes": strikes}

    # ── Journal ────────────────────────────────────────────────────────────────
    def journal(self):
        cutoff = self.now - timedelta(days=JOURNAL_DAYS)
        groups = OrderedDict()
        for h in sorted(self.closed, key=lambda h: str(h.get("exit_ts", "")), reverse=True):
            t = parse_ts(h.get("exit_ts"))
            if not t or t < cutoff:
                continue
            key = (h.get("instrument_name"), h.get("exit_reason"), str(h.get("exit_ts", ""))[:16])
            g = groups.setdefault(key, {"ts": iso(t), "instrument": key[0], "reason": key[1],
                                        "contracts": 0.0, "pnl": 0.0, "premium": 0.0, "lots": 0})
            c = fnum(h.get("contracts"), 1)
            g["contracts"] = round(g["contracts"] + c, 4)
            g["pnl"] = round(g["pnl"] + fnum(h.get("pnl_usd")), 2)
            g["premium"] = round(g["premium"] + fnum(h.get("entry_price")) * fnum(h.get("entry_spot")) * c, 2)
            g["lots"] += 1
        hedge = []
        for e in sorted(self.hedge.get("history", []) or [], key=lambda e: str(e.get("ts", "")), reverse=True)[:25]:
            hedge.append({"ts": iso(parse_ts(e.get("ts"))), "side": e.get("side"), "qty": fnum(e.get("qty")),
                          "price": fnum(e.get("spot")), "qty_after": fnum(e.get("qty_after")),
                          "realized": fnum(e.get("realized_pnl_usd"), None) if e.get("realized_pnl_usd") is not None else None})
        return {"closed": list(groups.values()), "hedge": hedge}

    # ── Routine : décisions en attente ─────────────────────────────────────────
    def decisions(self):
        recos = []
        for s in self.routine.get("sweeps", []) or []:
            if not s.get("recommend_change"):
                continue
            cur = next((r for r in s.get("results", []) if r.get("is_current")), {})
            recos.append({"param": s.get("param"), "current": cur.get("label"),
                          "proposed": s.get("opt_label") or s.get("best_label"),
                          "gain": s.get("gain_vs_current"), "wins": s.get("fold_wins"), "n": s.get("n_folds")})
        inert = [s.get("param") for s in self.routine.get("sweeps", []) or []
                 if fnum(s.get("sensitivity")) < 0.1 and s.get("kind", "param") == "param"]
        return {"generated_at": iso(parse_ts(self.routine.get("generated_at"))), "recommendations": recos,
                "inert": inert, "n_sweeps": len(self.routine.get("sweeps", []) or [])}

    # ── Verdict ────────────────────────────────────────────────────────────────
    def verdict(self, bot_age, book_rows, book_tot, cb, gates, decisions):
        items = []

        def add(level, text):
            items.append({"level": level, "text": text})

        if bot_age is None:
            add("critical", "Aucune trace du dernier run du bot (scan_entry.json absent).")
        elif bot_age >= BOT_STALE_ACTION_H:
            add("critical", f"Le bot n'a pas tourné depuis {bot_age:.0f} h — vérifier GitHub Actions.")
        elif bot_age >= BOT_STALE_WARN_H:
            add("warning", f"Dernier run il y a {bot_age:.1f} h (GitHub retarde parfois le cron horaire).")

        if book_rows and abs(book_tot["drift_btc"]) > book_tot["threshold_btc"]:
            add("critical", f"Delta non couvert {book_tot['drift_btc']:+.3f} BTC > seuil {book_tot['threshold_btc']:.3f} "
                            "— le rebalance aurait dû partir.")
        if cb["state"] == "risk_off":
            add("warning", f"Circuit breaker total actif : book fermé. Re-entrée si {cb['restore_rule']}.")
        elif cb["state"] == "reduced":
            add("warning", f"Allègement CB actif : entrées gelées jusqu'à {cb['restore_rule']}.")
        for m in cb["meters"]:
            if m["level"] == "warning" and book_rows:
                add("warning", f"{m['label']} {m['value']:+.1f}{m['unit']} : on approche d'un palier du circuit breaker.")
            elif m["level"] == "critical" and book_rows and cb["state"] == "armed":
                add("critical", f"{m['label']} {m['value']:+.1f}{m['unit']} a franchi un seuil CB — vérifier que le bot a réagi.")
        for r in book_rows:
            if r["roll_watch"]:
                add("warning", f"{r['instrument']} : TTE {r['tte_days']:.2f} j et gamma {r['gamma_pts']:.1f} pts — roll attendu.")
        if self.recon and self.recon.get("n_mismatched"):
            add("critical", f"Réconciliation Deribit : {self.recon['n_mismatched']} écart(s) entre le bot et le compte réel.")
        if decisions["recommendations"]:
            n = len(decisions["recommendations"])
            add("info", f"{n} changement{'s' if n > 1 else ''} de paramètre recommandé{'s' if n > 1 else ''} "
                        "par la routine — décision humaine requise.")

        levels = [i["level"] for i in items]
        if "critical" in levels:
            level, head = "critical", "Action requise"
        elif "warning" in levels:
            level, head = "warning", "À surveiller"
        elif book_rows:
            level, head = "good", "Tout est sous contrôle"
        else:
            level, head = "idle", "Rien à faire"

        if gates["open"]:
            entry = "Entrées ouvertes : le bot peut prendre une position au prochain run."
        else:
            entry = f"Pas d'entrée — {gates['why']}"
            if gates["blocked_key"] == "score" and gates.get("limiting"):
                entry += f" ; composante la plus faible : {gates['limiting']['component']}"
            entry += "."
        book = (f"{len(book_rows)} position{'s' if len(book_rows) > 1 else ''} · "
                f"{book_tot['contracts']:.2f} BTC" if book_rows else "Book à plat")
        return {"level": level, "headline": head, "book": book, "entry": entry, "items": items}

    # ── Assemblage ─────────────────────────────────────────────────────────────
    def build(self):
        bot_ts = parse_ts(self.scan.get("ts"))
        bot_age = (self.now - bot_ts).total_seconds() / 3600 if bot_ts else None
        rows, tot = self.book()
        scan_rows, weights = self.scanner()
        gates = self.gates(tot, scan_rows, weights)
        cb = self.circuit_breaker()
        dec = self.decisions()
        perf = self.performance(tot)
        stress = self.stress(rows)
        market = self.market_series(rows)
        for r in rows:
            r.pop("_sigma", None); r.pop("_tte_y", None)
        mc = self.mc
        params = {k: self.p(k) for k in ("ENTRY_SCORE_MIN", "DVOL_MIN", "MAX_PORTFOLIO_BTC", "RANK_FLOOR",
                                          "SIZE_CONVEXITY", "MIN_PREMIUM_USD", "CB_T1_MOVE_1D_PCT",
                                          "CB_T1_MOVE_3D_PCT", "CB_T1_KEEP", "CB_T1_RESTORE_MOVE_PCT",
                                          "CB_MOVE_3D_PCT", "CB_DVOL_3D_PTS", "SCORE_W_IVHV",
                                          "SCORE_W_YIELD", "SCORE_W_SKEW")}
        return {
            "meta": {"generated_at": iso(self.now), "bot_ts": iso(bot_ts),
                     "bot_age_h": round(bot_age, 2) if bot_age is not None else None,
                     "mode": ("live · " + self.recon.get("env", "?")) if self.recon else "paper",
                     "repo": REPO_URL, "params": params},
            "market": {"spot": self.spot, "dvol": fnum(mc.get("curr_iv"), None), "dvol_1d": mc.get("dvol_1d_chg"),
                       "hv_blend": fnum(mc.get("hv_blend"), None), "hv10": fnum(mc.get("hv_10d"), None),
                       "hv30": fnum(mc.get("hv_30d"), None), "iv_hv": fnum(mc.get("iv_hv_ratio"), None),
                       "iv_rank": fnum(mc.get("iv_rank"), None), "regime": mc.get("regime"),
                       "move_1d": mc.get("cb_move_1d"), "move_3d": mc.get("cb_move_3d"),
                       "hv5": mc.get("cb_hv_5d")},
            "verdict": self.verdict(bot_age, rows, tot, cb, gates, dec),
            "book": {"rows": rows, "totals": tot}, "stress": stress, "cb": cb, "gates": gates,
            "scanner": {"rows": scan_rows, "weights": weights, "threshold": gates["threshold"]},
            "performance": perf, "series": market, "journal": self.journal(), "decisions": dec,
            "reconcile": self.recon,
        }


ASSETS = HERE / "dashboard_assets"


def render(model: dict, template: Path) -> str:
    """Template + CSS/JS communs (dashboard_assets/) + modèle JSON embarqué."""
    html = template.read_text(encoding="utf-8")
    html = html.replace("/*__COMMON_CSS__*/", (ASSETS / "common.css").read_text(encoding="utf-8"))
    html = html.replace("/*__COMMON_JS__*/", (ASSETS / "common.js").read_text(encoding="utf-8"))
    payload = json.dumps(model, ensure_ascii=False, separators=(",", ":"), default=str)
    payload = payload.replace("</", "<\\/")   # pas de fermeture de balise dans le JSON embarqué
    return html.replace("/*__DASHBOARD_DATA__*/null", payload)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(HERE))
    ap.add_argument("--out", default=str(HERE / "docs" / "v2.html"))
    ap.add_argument("--now", default=None, help="horodatage de référence (tests)")
    args = ap.parse_args()
    now = parse_ts(args.now) if args.now else datetime.now(timezone.utc)
    model = Model(Path(args.data_dir), now).build()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(model, TEMPLATE), encoding="utf-8")
    v = model["verdict"]
    print(f"{out.name} généré ({out.stat().st_size / 1024:.0f} KB) — {v['headline']} · {v['book']}")


if __name__ == "__main__":
    main()
