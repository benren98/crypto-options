"""
generate_html.py — Génère docs/index.html depuis pnl_summary.json + positions.json + positions_detail.json
Appelé par GitHub Actions après chaque snapshot.
"""
import json, math
from pathlib import Path
from datetime import datetime, timezone, timedelta

# ── Timezone NY ────────────────────────────────────────────────────────────────
try:
    from zoneinfo import ZoneInfo
    _TZ_NY = ZoneInfo("America/New_York")
except Exception:
    _TZ_NY = None

def to_ny(ts_str) -> str:
    if not ts_str or ts_str == "—":
        return str(ts_str)
    if isinstance(ts_str, datetime):
        dt = ts_str if ts_str.tzinfo else ts_str.replace(tzinfo=timezone.utc)
    else:
        s = str(ts_str).strip()
        dt = None
        for fmt, val in [
            ("%Y-%m-%d %H:%M:%S UTC", s),
            ("%Y-%m-%d %H:%M UTC",    s),
            ("%Y-%m-%dT%H:%M:%S+00:00", s),
            ("%Y-%m-%d %H:%M:%S",     s.replace(" UTC", "")),
            ("%Y-%m-%d %H:%M",        s.replace(" UTC", "")),
        ]:
            try:
                dt = datetime.strptime(val, fmt).replace(tzinfo=timezone.utc); break
            except ValueError:
                continue
        if dt is None:
            return s
    if _TZ_NY:
        dt_ny = dt.astimezone(_TZ_NY)
        label = dt_ny.strftime("%Z")
    else:
        year = dt.year
        mar8  = datetime(year, 3,  8, tzinfo=timezone.utc)
        nov1  = datetime(year, 11, 1, tzinfo=timezone.utc)
        dst_start = mar8 + timedelta(days=(6 - mar8.weekday()) % 7, hours=2)
        dst_end   = nov1 + timedelta(days=(6 - nov1.weekday()) % 7, hours=2)
        in_dst  = dst_start <= dt < dst_end
        dt_ny   = dt + timedelta(hours=-4 if in_dst else -5)
        label   = "EDT" if in_dst else "EST"
    return dt_ny.strftime(f"%Y-%m-%d %H:%M {label}")

# ── Lecture des données ────────────────────────────────────────────────────────
pos_raw  = json.loads(Path("positions.json").read_text())
summ_raw = json.loads(Path("pnl_summary.json").read_text())

positions_list = pos_raw.get("positions") or ([pos_raw["open"]] if pos_raw.get("open") else [])
hedge_data     = pos_raw.get("hedge", {})
hist           = pos_raw.get("history", [])

# positions_detail : données live par position (depuis pnl_monitor.py)
pd_file = Path("positions_detail.json")
positions_detail = json.loads(pd_file.read_text()) if pd_file.exists() else []
# pd_map : 1 entrée par instrument. ATTENTION : en cas de ré-entrée (2 lots du même
# instrument), le dernier lot écrase le premier → ne l'utiliser QUE pour des champs
# instrument-level identiques entre lots (mark, IV, tte). Pour le money/greeks par lot,
# utiliser _match_live(p) qui apparie chaque lot à son détail exact.
pd_map = {d.get("instrument"): d for d in positions_detail}

# Détails live groupés par instrument (gère la ré-entrée : plusieurs lots même nom)
_pd_lots: dict = {}
for _d in positions_detail:
    _pd_lots.setdefault(_d.get("instrument"), []).append(_d)

def _synth_live(p: dict) -> dict:
    """Détail live synthétique pour un lot pas encore marké par pnl_monitor
    (cas : ré-entrée ajoutée par greeks_hedge après le run pnl_monitor → positions_detail
    en retard d'un cycle). PnL = 0, greeks d'entrée. Honnête jusqu'au prochain snapshot."""
    c  = float(p.get("contracts", 1))
    em = float(p.get("entry_mark_price", p.get("entry_price", 0)))
    return {
        "instrument":         p.get("instrument_name", ""),
        "current_price_btc":  em, "current_bid_btc": em, "current_ask_btc": em,
        "current_iv_pct":     float(p.get("iv_at_entry", 0)),
        "entry_price_btc":    float(p.get("entry_price", 0)),
        "entry_spot":         float(p.get("entry_spot", 0)),
        "pnl_option_usd":     0.0, "pnl_option_btc": 0.0,
        "live_delta":         float(p.get("delta_at_entry", 0)) * c,
        "live_gamma":         float(p.get("gamma_at_entry", 0)) * c,
        "live_vega":          float(p.get("vega_at_entry", 0)) * c,
        "theta_theory_usd":   0.0, "theta_daily_now_usd": 0.0, "days_held": 0.0,
        "_synthetic":         True,
    }

# Assignation un-à-un lot↔détail (chaque détail utilisé au plus une fois).
# Sans ça, 2 lots du même instrument apparieraient le même détail → double-comptage.
_lot_live: dict = {}
for _instr, _lots in {n: [p for p in positions_list if p.get("instrument_name") == n]
                      for n in {p.get("instrument_name") for p in positions_list}}.items():
    _avail = list(_pd_lots.get(_instr, []))
    for _p in _lots:
        if not _avail:
            _lot_live[id(_p)] = _synth_live(_p)   # lot pas encore marké
            continue
        _ep, _es = float(_p.get("entry_price", 0)), float(_p.get("entry_spot", 0))
        _best = min(_avail, key=lambda d: abs(float(d.get("entry_price_btc", 0)) - _ep)
                    + abs(float(d.get("entry_spot", 0)) - _es) / 1e5)
        _lot_live[id(_p)] = _best
        _avail.remove(_best)

def _match_live(p: dict) -> dict:
    """Détail live d'un lot (assignation un-à-un pré-calculée). Jamais le même détail
    pour deux lots ; un lot non encore marké reçoit un détail synthétique à l'entrée."""
    return _lot_live.get(id(p), _pd_lots.get(p.get("instrument_name", ""), [{}])[0] if _pd_lots.get(p.get("instrument_name", "")) else {})

def _group_positions() -> list:
    """Regroupe positions_list par instrument (ordre préservé). Retourne une liste de
    listes de lots — 1 lot le plus souvent, 2+ après une ré-entrée."""
    from collections import OrderedDict
    groups: OrderedDict = OrderedDict()
    for p in positions_list:
        groups.setdefault(p.get("instrument_name", ""), []).append(p)
    return list(groups.values())

# Compteurs distincts : instruments uniques (ce que l'utilisateur appelle "positions")
# vs lots (positions_list inclut les ré-entrées comme lots séparés)
n_instruments = len({p.get("instrument_name") for p in positions_list})
n_lots        = len(positions_list)
_lots_suffix  = f" ({n_lots} lots)" if n_lots > n_instruments else ""

# scan_entry : top 5 opportunités (depuis greeks_hedge.py --run)
se_file = Path("scan_entry.json")
scan_entry = json.loads(se_file.read_text()) if se_file.exists() else {}
_se_spot = float(scan_entry.get("market_context", {}).get("spot", 0)) or spot

# Historique pour graphiques
history_file = Path("pnl_history.json")
pnl_history  = json.loads(history_file.read_text()) if history_file.exists() else []

no_position = not positions_list and not hist

# ── Helpers ────────────────────────────────────────────────────────────────────
def f(v, decimals=2, sign=False):
    try:
        n = float(v)
        fmt = f"{{:+,.{decimals}f}}" if sign else f"{{:,.{decimals}f}}"
        return fmt.format(n)
    except (TypeError, ValueError):
        return str(v) if v else "—"

def color(v, invert=False):
    try:
        n = float(v)
        if invert: n = -n
        return "pos" if n > 0 else ("neg" if n < 0 else "neu")
    except:
        return "neu"

def row(label, value, cls=""):
    return f'<tr><td class="label">{label}</td><td class="{cls}">{value}</td></tr>'

def srow(label, val, invert=False, decimals=0):
    v  = f(val, decimals, sign=True)
    cl = color(val, invert)
    return f'<tr><td class="label">{label}</td><td class="val {cl}">{v} $</td></tr>'

# ── Données portfolio ──────────────────────────────────────────────────────────
s = summ_raw
spot           = float(s.get("spot", 0))
hedge_qty      = float(s.get("hedge_qty", hedge_data.get("qty", 0)))
hedge_avg      = float(hedge_data.get("avg_entry", 0))
hedge_thr_pct  = float(s.get("hedge_threshold_pct") or 5.0)
hedge_thr_btc  = float(s.get("hedge_threshold_btc") or hedge_thr_pct / 100)
# Recalcule le drift depuis les données fraîches (positions.json hedge qty mis à jour par greeks_hedge
# après pnl_monitor, donc plus récent que pnl_summary.hedge_delta_drift)
_net_opt_delta = sum(
    float(_match_live(p).get(
        "live_delta",
        p.get("delta_at_entry", 0) * float(p.get("contracts", 1))
    ))
    for p in positions_list
)
_fresh_hedge_qty = float(hedge_data.get("qty", 0))
_drift         = -_net_opt_delta - abs(_fresh_hedge_qty)   # résidu non-hedgé (BTC)
_drift_abs     = abs(_drift)
_drift_pct     = _drift_abs * 100
_fill_pct      = min(100.0, _drift_abs / max(hedge_thr_btc, 1e-9) * 100)
_bar_color     = "#f85149" if _drift_abs > hedge_thr_btc else ("#d29922" if _fill_pct > 70 else "#3fb950")
_drift_cl      = "warn" if _drift_abs > hedge_thr_btc else ("warn" if _fill_pct > 70 else "ok")

# PnL cumulé (historique + ouvert)
pnl_hist_total = sum(float(h.get("pnl_usd", 0)) for h in hist)
pnl_open       = float(s.get("total_pnl_usd", 0))
pnl_cumul      = pnl_hist_total + pnl_open
realized_hedge = float(hedge_data.get("realized_pnl_usd", 0))
pnl_hedge_usd  = float(s.get("pnl_hedge_usd", 0))
_funding_usd   = float(s.get("funding_pnl_usd", 0) or 0)
# PnL LATENT (mark-to-market des positions ouvertes) = option latente + hedge FLOTTANT
# (on exclut le hedge réalisé qui est déjà encaissé). Calculé EXACTEMENT comme le
# "LATENT TOTAL" de la carte détail (somme _match_live par lot) → header == détail.
# L'ancien total_pnl_usd mélangeait option latente + hedge réalisé+flottant → incohérent.
_pnl_opt_latent = sum(float(_match_live(p).get("pnl_option_usd", 0)) for p in positions_list)
pnl_open_latent = _pnl_opt_latent + (pnl_hedge_usd - realized_hedge) + _funding_usd
# Réalisé cumulé (options clôturées + rebalancements hedge) et total stratégie — mêmes
# définitions que la carte détail (TOTAL RÉALISÉ / TOTAL STRATÉGIE)
pnl_realized_total = pnl_hist_total + realized_hedge
pnl_strategy_total = pnl_open_latent + pnl_realized_total

# Deltas depuis dernier snapshot
_prev_spot  = float(pnl_history[-2].get("spot",      0)) if len(pnl_history) >= 2 else None
_prev_pnl   = float(pnl_history[-2].get("pnl_total", 0)) if len(pnl_history) >= 2 else None
_curr_spot  = float(s.get("spot", 0))
_curr_pnl   = float(s.get("total_pnl_usd", 0))
_delta_spot = (_curr_spot - _prev_spot)          if _prev_spot else None
_delta_spot_pct = (_delta_spot / _prev_spot * 100) if _prev_spot else None
_delta_pnl  = (_curr_pnl  - _prev_pnl)           if _prev_pnl  is not None else None

# Move spot sur 4h et 1j — cherche le snapshot le plus proche dans le temps
def _parse_ts(ts_str) -> datetime | None:
    if not ts_str:
        return None
    s = str(ts_str).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%d %H:%M UTC",
                "%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s.replace(" UTC", ""), fmt.replace(" UTC", "")).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None

def _spot_move(target_hours: float):
    """Retourne (move_abs, move_pct) vs le snapshot le plus proche de target_hours en arrière."""
    if not pnl_history:
        return None, None
    now_utc = datetime.now(timezone.utc)
    target_dt = now_utc - timedelta(hours=target_hours)
    best, best_diff = None, None
    for p in pnl_history[:-1]:  # exclure le dernier (= snapshot courant)
        dt = _parse_ts(p.get("ts"))
        if dt is None:
            continue
        diff = abs((dt - target_dt).total_seconds())
        if best_diff is None or diff < best_diff:
            best, best_diff = p, diff
    if best is None or best_diff > 3 * 3600:  # pas de snapshot dans ±3h de la cible
        return None, None
    ref = float(best.get("spot", 0))
    if ref == 0:
        return None, None
    return _curr_spot - ref, (_curr_spot - ref) / ref * 100

_move4h_abs,  _move4h_pct  = _spot_move(4)
_move1d_abs,  _move1d_pct  = _spot_move(24)

ts        = to_ny(s.get("timestamp", "—"))
generated = to_ny(datetime.now(timezone.utc))

# ── CSS & layout ───────────────────────────────────────────────────────────────
CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'SF Mono', 'Fira Code', monospace; background: #0d1117; color: #e6edf3; min-height: 100vh; padding: 24px; }
h1 { font-size: 1.4rem; color: #58a6ff; margin-bottom: 10px; }
.subtitle { color: #8b949e; font-size: 0.82rem; margin-bottom: 16px; }
.section-title { font-size: 0.78rem; text-transform: uppercase; letter-spacing: .1em; color: #8b949e;
  border-bottom: 1px solid #21262d; padding-bottom: 8px; margin: 20px 0 12px; }

/* chips */
.header-bar { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 20px; align-items: center; }
.chip { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 8px 14px;
  font-size: 0.82rem; display: flex; flex-direction: column; gap: 2px; min-width: 140px; }
.chip-label { color: #8b949e; font-size: 0.70rem; text-transform: uppercase; letter-spacing: .06em; }
.chip-value { font-weight: 700; font-size: 1.05rem; color: #e6edf3; }
.chip-delta { font-size: 0.75rem; margin-top: 1px; }

/* grid */
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 16px; }
.grid-3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
.full { grid-column: 1 / -1; }

/* cards */
.card { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 20px; }
.card h2 { font-size: 0.78rem; text-transform: uppercase; letter-spacing: .1em; color: #8b949e;
  border-bottom: 1px solid #21262d; padding-bottom: 10px; margin-bottom: 14px; }
.total-card { border-color: #388bfd44; }
.pos2-card  { border-color: #3fb95033; }

/* tables */
table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
td { padding: 5px 4px; vertical-align: top; }
td.label { color: #8b949e; width: 50%; }
td.val { text-align: right; font-weight: 600; }
.tbl { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
.tbl th { color: #8b949e; font-weight: 500; padding: 6px 8px; text-align: right;
  border-bottom: 1px solid #21262d; white-space: nowrap; }
.tbl th:first-child { text-align: left; }
.tbl td { padding: 5px 8px; text-align: right; border-bottom: 1px solid #1c2128; }
.tbl td:first-child { text-align: left; color: #e6edf3; }
.tbl tr.hl { background: #1c2128; }
.tbl tr:hover { background: #21262d; }
.tbl td.muted { color: #8b949e; }
.tbl td.left { text-align: left; }

/* colors */
.pos { color: #3fb950; }
.neg { color: #f85149; }
.neu { color: #e6edf3; }
.warn { color: #d29922; }
.ok  { color: #3fb950; }
.big { font-size: 1.6rem; font-weight: 700; }

/* drift bar */
.progress-bg { background: #21262d; border-radius: 4px; height: 6px; margin-top: 6px; }
.progress-fill { border-radius: 4px; height: 6px; }

/* hist rows */
.hist-row { display: flex; justify-content: space-between; padding: 6px 0;
  border-bottom: 1px solid #21262d; font-size: 0.85rem; }
.hist-row:last-child { border-bottom: none; }

/* charts */
.chart-section { margin-top: 16px; display: flex; flex-direction: column; gap: 16px; }
.chart-card { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 20px; }
.chart-card h2 { font-size: 0.78rem; text-transform: uppercase; letter-spacing: .1em; color: #8b949e;
  border-bottom: 1px solid #21262d; padding-bottom: 10px; margin-bottom: 14px; }
.chart-wrap { position: relative; height: 360px; }

footer { text-align: center; color: #484f58; font-size: 0.75rem; margin-top: 28px; }
"""

# ── Barre drift ────────────────────────────────────────────────────────────────
drift_bar_html = f"""
<div style="margin-top:12px">
  <div style="display:flex;justify-content:space-between;font-size:0.75rem;color:#8b949e;margin-bottom:4px">
    <span>Drift actuel&nbsp;: <b class="{_drift_cl}">{f(_drift_pct,2)}%&thinsp;&#916;</b></span>
    <span>Seuil&nbsp;: {f(hedge_thr_pct,1)}%&thinsp;&#916;</span>
  </div>
  <div style="background:#21262d;border-radius:4px;height:8px;position:relative;overflow:hidden">
    <div style="height:8px;border-radius:4px;width:{_fill_pct:.1f}%;background:{_bar_color}"></div>
  </div>
  <div style="display:flex;justify-content:space-between;font-size:0.7rem;color:#484f58;margin-top:3px">
    <span>0%</span><span>{f(hedge_thr_pct,1)}% &#128308;</span>
  </div>
</div>"""

# ── Attribution PnL par position ───────────────────────────────────────────────
def _attr_lot_nums(p: dict, live: dict) -> dict:
    """Composantes d'attribution PnL d'UN lot (toutes en $, exactes et additives)."""
    entry_spot = float(p.get("entry_spot", spot))
    entry_p    = float(p.get("entry_price", 0))
    entry_mark = float(p.get("entry_mark_price", entry_p))
    entry_iv   = float(p.get("iv_at_entry", 0))
    contracts  = float(p.get("contracts", 1))
    gamma_e    = float(p.get("gamma_at_entry", 7e-5)) * contracts
    curr_mark  = float(live.get("current_price_btc", entry_mark))
    curr_ask   = float(live.get("current_ask_btc",   curr_mark))
    curr_iv    = float(live.get("current_iv_pct", entry_iv))
    d_live     = float(live.get("live_delta", p.get("delta_at_entry", 0) * contracts))
    vega_live  = float(live.get("live_vega", p.get("vega_at_entry", 0) * contracts))
    days_held  = float(live.get("days_held", 0))
    theta_d    = float(live.get("theta_daily_now_usd", 0))
    ds         = spot - entry_spot
    div        = curr_iv - entry_iv
    pnl_delta  = abs(d_live) * ds
    pnl_gamma  = 0.5 * (-gamma_e) * ds ** 2
    pnl_theta  = theta_d * days_held
    pnl_vega   = (-vega_live) * div
    mid_mid    = (entry_mark * entry_spot - curr_mark * spot) * contracts
    ba_entry   = -(entry_mark - entry_p) * entry_spot * contracts
    ba_exit    = -(curr_ask - curr_mark) * spot * contracts
    return {"pnl_delta": pnl_delta, "pnl_gamma": pnl_gamma, "pnl_theta": pnl_theta,
            "pnl_vega": pnl_vega, "mid_mid": mid_mid, "ba_entry": ba_entry, "ba_exit": ba_exit}


def _attr_card(lots: list) -> str:
    """Carte attribution PnL. Plusieurs lots (ré-entrée) → décomposition sommée par lot
    (exacte), scalaires d'en-tête en VWAP. 1 lot → comportement standard."""
    p, live = _agg_lots(lots)
    _n_lots = p.get("_n_lots", 1)
    instr      = p.get("instrument_name","—")
    entry_spot = float(p.get("entry_spot", spot))
    entry_p    = float(p.get("entry_price", 0))
    entry_mark = float(p.get("entry_mark_price", entry_p))
    entry_iv   = float(p.get("iv_at_entry", 0))
    contracts  = float(p.get("contracts", 1))

    curr_mark  = float(live.get("current_price_btc", entry_mark))
    curr_iv    = float(live.get("current_iv_pct", entry_iv))
    days_held  = float(live.get("days_held", 0))
    theta_d       = float(live.get("theta_daily_now_usd", 0))
    theta_theory  = float(live.get("theta_theory_usd", 0))
    vrp_capture   = float(live.get("vrp_capture_pct", float("nan"))) if live.get("vrp_capture_pct") not in (None, "", "nan") else float("nan")
    pnl_opt    = float(live.get("pnl_option_usd", 0))

    ds         = spot - entry_spot
    div        = curr_iv - entry_iv

    # Composantes : somme exacte des décompositions par lot (jamais un lot synthétique)
    _nums = [_attr_lot_nums(_p, _match_live(_p)) for _p in lots]
    pnl_delta = sum(n["pnl_delta"] for n in _nums)
    pnl_gamma = sum(n["pnl_gamma"] for n in _nums)
    pnl_theta = sum(n["pnl_theta"] for n in _nums)
    pnl_vega  = sum(n["pnl_vega"]  for n in _nums)
    mid_mid   = sum(n["mid_mid"]   for n in _nums)
    ba_entry  = sum(n["ba_entry"]  for n in _nums)
    ba_exit_est = sum(n["ba_exit"] for n in _nums)
    pnl_resid = mid_mid - (pnl_delta + pnl_gamma + pnl_theta + pnl_vega)
    total_opt = mid_mid + ba_entry
    # Prime encaissée exacte (Σ par lot)
    prem_total = float(p.get("_premium_usd_total", entry_p * entry_spot * contracts))

    # TTE fallback: compute from expiry_dt when live data not yet available
    _tte_live = live.get("tte_days")
    if _tte_live is not None:
        tte = float(_tte_live)
    else:
        try:
            _exp_dt = datetime.fromisoformat(p.get("expiry_dt","").replace("+00:00","")).replace(tzinfo=timezone.utc)
            tte = max(0.0, (_exp_dt - datetime.now(timezone.utc)).total_seconds() / 86400)
        except Exception:
            tte = 0.0
    cl_tte     = "warn" if tte <= 1 else "neu"
    _lot_tag = (f' <span class="warn" style="font-size:0.7rem">⊕ {_n_lots} lots (VWAP)</span>' if _n_lots > 1 else "")

    return f"""<div class="card">
  <h2>Attribution PnL — {instr}{_lot_tag}</h2>
  <div style="font-size:0.75rem;color:#8b949e;margin-bottom:10px">
    Spot {f(ds,0,True)}$&nbsp;·&nbsp;IV {f(div,1,True)}pts&nbsp;·&nbsp;{f(days_held*24,1)}h tenu&nbsp;·&nbsp;TTE <span class="{cl_tte}">{f(tte,2)}j</span>
    &nbsp;·&nbsp;Prime encaissée <span style="color:#3fb950;font-weight:600">+{f(prem_total, 0)}$</span> <span style="color:#484f58">({f(contracts,1)} BTC{" agrégé" if _n_lots>1 else ""} × {f(entry_p,5)} BTC × ${f(entry_spot,0)})</span>
    <br>Mid actuel <b style="color:#e6edf3">{f(curr_mark,5)} BTC</b> (${f(curr_mark * spot, 0)} / 1 BTC)
    &nbsp;·&nbsp;<span style="color:#484f58">entrée mid {f(entry_mark,5)} BTC{" VWAP" if _n_lots>1 else ""}</span>
    &nbsp;·&nbsp;<span class="{color(entry_mark - curr_mark)}">{f((entry_mark - curr_mark) / entry_mark * 100 if entry_mark else 0, 1, True)}% depuis l'entrée</span>
  </div>
  <table class="tbl">
    <tr><th style="text-align:left">Composante</th><th>Valeur ($)</th><th>% PnL opt.</th></tr>
    {_attr_row("Δ Delta",  pnl_delta, pnl_opt)}
    {_attr_row("Γ Gamma",  pnl_gamma, pnl_opt)}
    {_attr_row("Θ Theta",  pnl_theta, pnl_opt)}
    {_attr_row("ν Vega",   pnl_vega,  pnl_opt)}
    {_attr_row("~ Résidu", pnl_resid, pnl_opt)}
    <tr style="border-top:1px solid #30363d">
      <td class="muted" style="font-size:0.78rem">= Mid / mid</td>
      <td class="{color(mid_mid)}">{f(mid_mid,0,True)}</td>
      <td class="muted">—</td>
    </tr>
    <tr>
      <td style="font-size:0.78rem">+ B/A entrée <span class="muted">(bid vs mark)</span></td>
      <td class="{color(ba_entry)}">{f(ba_entry,0,True)}</td>
      <td class="muted" style="font-size:0.78rem">{f(ba_entry/pnl_opt*100 if abs(pnl_opt)>0.01 else 0,0,True)}%</td>
    </tr>
    <tr style="border-top:1px solid #30363d;font-weight:600">
      <td>TOTAL OPTION (réalisable au bid)</td>
      <td class="{color(total_opt)}">{f(total_opt,0,True)}</td>
      <td class="muted">—</td>
    </tr>
    <tr style="border-top:1px solid #21262d">
      <td style="font-size:0.78rem;color:#8b949e">− B/A sortie estimé <span class="muted">(ask vs mark)</span></td>
      <td class="{color(ba_exit_est)}">{f(ba_exit_est,0,True)}</td>
      <td class="muted">—</td>
    </tr>
    <tr style="font-weight:600">
      <td>NET si rachat maintenant (ask)</td>
      <td class="{color(total_opt+ba_exit_est)}">{f(total_opt+ba_exit_est,0,True)}</td>
      <td class="muted">—</td>
    </tr>
  </table>
  {(lambda: f'''<table style="margin-top:10px;border-top:1px solid #21262d;padding-top:8px;width:100%">
    <tr><td colspan="2" style="color:#8b949e;font-size:0.75rem;padding-bottom:4px;padding-top:6px">THETA</td></tr>
    <tr>
      <td class="label">Théorique cumulé <span style="color:#484f58;font-size:0.72rem">(theta entrée × jours tenus)</span></td>
      <td class="val pos">+{f(theta_theory,0)}$</td>
    </tr>
    <tr>
      <td class="label">Theta actuel (live)</td>
      <td class="val pos">+{f(theta_d,0)}$/j</td>
    </tr>
    <tr>
      <td class="label">VRP capturé <span style="color:#484f58;font-size:0.72rem">(PnL opt / theta théo)</span></td>
      <td class="val {"pos" if not math.isnan(vrp_capture) and vrp_capture>=80 else "warn" if not math.isnan(vrp_capture) and vrp_capture>=0 else "neg"}">{f(vrp_capture,0)+"%" if not math.isnan(vrp_capture) else "—"} <span style="color:#484f58;font-size:0.72rem">(100%=tout capturé)</span></td>
    </tr>
  </table>''')()}
</div>"""

def _attr_row(label, val, total):
    pct = (val / total * 100) if abs(total) > 0.01 else 0
    return (f'<tr><td>{label}</td>'
            f'<td class="{color(val)}">{f(val,0,True)}</td>'
            f'<td class="{color(val)}" style="font-size:0.78rem">{f(pct,0,True)}%</td></tr>')

# ── Agrégation des lots (ré-entrée : plusieurs lots d'un même instrument) ───────
def _agg_lots(lots: list) -> tuple:
    """Agrège une liste de lots du même instrument en (position, détail live) affichables.
    Règles de correction :
      - money & greeks (pnl, delta/gamma/vega déjà scalés par contracts) → SOMME
      - prix d'entrée (price, spot, mark, IV) → VWAP pondéré par contracts
      - ratios (% prime, VRP) → RECALCULÉS depuis les sommes, jamais moyennés
      - entry_ts → le plus ancien ; entry_score → moyenne pondérée (label)
    Un seul lot : renvoie tel quel (aucune transformation)."""
    if len(lots) == 1:
        return lots[0], _match_live(lots[0])

    livs = [_match_live(p) for p in lots]
    cs   = [float(p.get("contracts", 1)) for p in lots]
    C    = sum(cs) or 1.0

    def wavg(vals):
        return sum(v * c for v, c in zip(vals, cs)) / C

    p0 = dict(lots[0])
    p0["contracts"]        = round(C, 4)
    p0["entry_price"]      = wavg([float(p.get("entry_price", 0)) for p in lots])
    p0["entry_spot"]       = wavg([float(p.get("entry_spot", spot)) for p in lots])
    p0["entry_mark_price"] = wavg([float(p.get("entry_mark_price", p.get("entry_price", 0))) for p in lots])
    p0["iv_at_entry"]      = wavg([float(p.get("iv_at_entry", 0)) for p in lots])
    p0["entry_ts"]         = min((p.get("entry_ts", "") for p in lots), default="")
    if any(p.get("entry_score") is not None for p in lots):
        p0["entry_score"]  = wavg([float(p.get("entry_score", 0) or 0) for p in lots])
    p0["entry_sizing_btc"] = round(C, 4)
    p0["_n_lots"]          = len(lots)
    # Prime exacte = Σ(prix × spot × contracts) par lot (≠ produit des VWAP)
    prem_total = sum(float(p.get("entry_price", 0)) * float(p.get("entry_spot", 0)) * c
                     for p, c in zip(lots, cs))
    p0["_premium_usd_total"] = prem_total

    nonempty = [d for d in livs if d]
    if nonempty:
        # base = champs instrument-level (mark, IV, tte) — préférer un détail RÉEL (marké)
        # à un détail synthétique (lot pas encore traité par pnl_monitor)
        _base = next((d for d in nonempty if not d.get("_synthetic")), nonempty[0])
        agg = dict(_base)
        agg["pnl_option_usd"]      = sum(float(d.get("pnl_option_usd", 0)) for d in nonempty)
        agg["pnl_option_btc"]      = sum(float(d.get("pnl_option_btc", 0)) for d in nonempty)
        agg["live_delta"]          = sum(float(d.get("live_delta", 0)) for d in nonempty)
        agg["live_gamma"]          = sum(float(d.get("live_gamma", 0)) for d in nonempty)
        agg["live_vega"]           = sum(float(d.get("live_vega", 0)) for d in nonempty)
        agg["theta_theory_usd"]    = sum(float(d.get("theta_theory_usd", 0)) for d in nonempty)
        agg["theta_daily_now_usd"] = sum(float(d.get("theta_daily_now_usd", 0)) for d in nonempty)
        agg["pnl_pct_of_premium"]  = round(agg["pnl_option_usd"] / prem_total * 100, 2) if prem_total else 0.0
        _tt = agg["theta_theory_usd"]
        agg["vrp_capture_pct"]     = round(agg["pnl_option_usd"] / _tt * 100, 1) if _tt > 0.5 else float("nan")
        agg["entry_spot"]          = p0["entry_spot"]
        agg["entry_price_btc"]     = p0["entry_price"]
        agg["entry_iv_pct"]        = p0["iv_at_entry"]
        agg["days_held"]           = wavg([float(d.get("days_held", 0)) for d in livs])
    else:
        agg = {}
    return p0, agg


# ── Tableau des positions ──────────────────────────────────────────────────────
def _positions_table() -> str:
    if not positions_list:
        return '<p style="color:#8b949e">Aucune position ouverte.</p>'
    _groups = _group_positions()
    rows = ""
    for _lots in _groups:
        p, live = _agg_lots(_lots)
        _n_lots = p.get("_n_lots", 1)
        instr   = p.get("instrument_name","—")
        strike  = int(p.get("strike", 0))
        expiry  = (p.get("expiry_dt","") or "")[:10]
        # TTE fallback: compute from expiry_dt when live data not yet available
        _tte_live = live.get("tte_days")
        if _tte_live is not None:
            tte = float(_tte_live)
        else:
            try:
                _exp_dt = datetime.fromisoformat(p.get("expiry_dt","").replace("+00:00","")).replace(tzinfo=timezone.utc)
                tte = max(0.0, (_exp_dt - datetime.now(timezone.utc)).total_seconds() / 86400)
            except Exception:
                tte = 0.0
        cl_tte  = "warn" if tte <= 1 else ("neu" if tte > 3 else "warn")
        entry_p = float(p.get("entry_price", 0))
        entry_s = float(p.get("entry_spot", spot))
        # mark/ask fallback to entry prices when live data not yet available
        _entry_mark = float(p.get("entry_mark_price", entry_p))
        curr_m  = float(live.get("current_price_btc", _entry_mark))
        ask     = float(live.get("current_ask_btc", _entry_mark))
        iv_e    = float(p.get("iv_at_entry", 0))
        iv_c    = float(live.get("current_iv_pct", iv_e))
        div     = iv_c - iv_e
        _c      = float(p.get("contracts", 1))
        d_live     = float(live.get("live_delta", p.get("delta_at_entry", 0) * _c))
        gamma      = float(live.get("live_gamma", p.get("gamma_at_entry", 0) * _c))
        vega       = float(live.get("live_vega",  p.get("vega_at_entry",  0) * _c))
        delta_pct  = d_live / _c * 100                       # % moyen par contrat
        gamma_pts  = (gamma / _c) * spot * 0.01 * 100        # pts Δ / 1% spot, par contrat
        pnl_opt = float(live.get("pnl_option_usd", 0))
        pnl_pct = float(live.get("pnl_pct_of_premium", 0))
        cl_pnl  = color(pnl_opt)
        cl_iv   = "neg" if div > 0 else "pos"
        moneyness = (strike / spot - 1) * 100
        cl_m    = "neg" if moneyness < 0 else "pos"
        entry_score   = p.get("entry_score")
        entry_sizing  = p.get("entry_sizing_btc")
        score_html    = f'<b>{f(entry_score,3)}</b>' if entry_score is not None else '<span class="muted">—</span>'
        sizing_html   = f'{f(entry_sizing,1)} BTC' if entry_sizing is not None else '<span class="muted">—</span>'
        _lot_badge = (f' <span class="warn" style="font-size:0.7rem" title="{_n_lots} lots agrégés (ré-entrée) — prix en VWAP, money/greeks en somme">⊕{_n_lots} lots</span>'
                      if _n_lots > 1 else "")
        _ts_cell = (f'{to_ny(p.get("entry_ts","—"))} <span class="muted" style="font-size:0.7rem">(+{_n_lots-1} ré-entrée)</span>'
                    if _n_lots > 1 else to_ny(p.get("entry_ts","—")))
        rows += f"""<tr>
      <td class="left"><b>{instr}</b>{_lot_badge}</td>
      <td class="left muted" style="font-size:0.78rem">{_ts_cell}</td>
      <td>${strike:,} <span class="{cl_m}" style="font-size:0.75rem">({f(moneyness,1,True)}%)</span></td>
      <td>{expiry}</td>
      <td class="{cl_tte}"><b>{f(tte,2)}j</b></td>
      <td>{f(entry_p,5)} <span class="muted" style="font-size:0.75rem">(${f(entry_p*entry_s,0)})</span></td>
      <td>{f(curr_m,5)} <span class="muted" style="font-size:0.75rem">(ask {f(ask,5)})</span></td>
      <td>{f(iv_e,1)}% → <b>{f(iv_c,1)}%</b> <span class="{cl_iv}" style="font-size:0.75rem">({f(div,1,True)}pts)</span></td>
      <td>{f(d_live,4)} <span class="muted" style="font-size:0.75rem">({f(delta_pct,1)}%)</span></td>
      <td>{f(gamma,6)} <span class="muted" style="font-size:0.75rem">({f(gamma_pts,2)} pts)</span> / <span style="font-size:0.75rem;color:#8b949e">{f(abs(vega),2)}$</span></td>
      <td class="{cl_pnl}"><b>{f(pnl_opt,0,True)}$</b></td>
      <td class="{cl_pnl}">{f(pnl_pct,1,True)}%</td>
      <td style="text-align:center">{score_html}</td>
      <td style="text-align:center">{sizing_html}</td>
    </tr>"""
    _n_instr = len(_groups)
    _n_extra = len(positions_list) - _n_instr
    _agg_note = (f' <span style="font-weight:400;color:#484f58;font-size:0.72rem">'
                 f'({_n_instr} instrument(s) · {_n_extra} ré-entrée(s) agrégée(s))</span>'
                 if _n_extra > 0 else "")
    return f"""<div class="card full">
  <h2>📍 Positions ouvertes — {_n_instr} position(s){_agg_note}</h2>
  <div style="overflow-x:auto">
  <table class="tbl">
    <tr>
      <th style="text-align:left">Instrument</th>
      <th style="text-align:left">Entrée (NY)</th>
      <th>Strike (moneyness)</th>
      <th>Expiry</th>
      <th>TTE</th>
      <th>Prime encaissée</th>
      <th>Mark / Ask actuel</th>
      <th>IV entrée → actuelle</th>
      <th>Delta</th>
      <th>Gamma / Vega</th>
      <th>PnL option</th>
      <th>% prime</th>
      <th>Score entrée</th>
      <th>Sizing</th>
    </tr>
    {rows}
  </table>
  </div>
</div>"""

# ── Tableau Greeks nets ────────────────────────────────────────────────────────
def _greeks_card() -> str:
    net_delta  = float(s.get("live_delta", 0))
    net_gamma  = float(s.get("live_gamma", 0))  # Σ(gamma_i × contracts_i) from pnl_monitor
    net_vega   = float(s.get("live_vega",  0))
    net_theta  = float(s.get("theta_daily_now_usd", 0))
    # gamma_pts = weighted average rate (pts Δ / 1% spot move) across positions
    # = net_gamma / total_contracts — consistent with per-position display
    _total_contracts = sum(float(p.get("contracts", 1)) for p in positions_list) or 1
    gamma_pts  = abs(net_gamma) / _total_contracts * spot * 0.01 * 100
    # delta_pct = weighted average across positions (= net_delta_BTC / total_contracts × 100)
    # net_delta in BTC is the correct sum for hedging; % is the avg rate per BTC notional
    delta_pct  = abs(net_delta) / _total_contracts * 100
    reb_count  = hedge_data.get("rebalances", 0)

    hh = hedge_data.get("history", [])
    last_reb = hh[-1] if hh else None
    last_reb_html = (
        f'<span class="ok">{to_ny(last_reb["ts"])} — '
        f'{"BUY" if last_reb.get("qty",0)>0 else "SELL"} '
        f'{f(abs(last_reb.get("qty",0)),5)} BTC @ ${f(last_reb.get("spot",0),0)}'
        f'<br><span style="font-size:0.75rem;color:#8b949e">'
        f'Qty: {f(last_reb.get("qty_before",0),5)} → {f(last_reb.get("qty_after",0),5)} BTC'
        f'  · VWAP: ${f(last_reb.get("vwap_before",0),2)} → ${f(last_reb.get("vwap_after",0),2)}'
        + (f'  · {last_reb["note"]}' if last_reb and last_reb.get("note") else "")
        + '</span></span>'
    ) if last_reb else '<span class="neu">—</span>'

    return f"""<div class="card">
  <h2>📐 Greeks nets portfolio</h2>
  <table>
    <tr><td colspan="2" style="color:#8b949e;font-size:0.75rem;padding-bottom:4px">OPTIONS (cumulé {n_instruments} pos.{_lots_suffix})</td></tr>
    {row("Δ Delta net",   f'<b>{f(net_delta,4)}</b>  ({f(delta_pct,1)}%)')}
    {row("Γ Gamma net",   f'<span class="neg">−{f(gamma_pts,2)} pts Δ / 1% move</span>')}
    {row("ν Vega net",    f'<span class="neg">{f(net_vega,2)}</span>  ({f(-net_vega,0)}$ / +1pt IV)')}
    {row("Θ Theta net",   f'<span class="pos">+${f(net_theta,0)}/jour</span>')}
    <tr><td colspan="2" style="color:#8b949e;font-size:0.75rem;padding-top:10px;padding-bottom:4px">HEDGE PERP</td></tr>
    {row("Qty short",     f'{f(hedge_qty,5)} BTC')}
    {row("VWAP entrée",   f'${f(hedge_avg,0)}')}
    {row("Rebalancements", str(reb_count))}
    {row("Delta net (pos+hedge)",
         f'<b class="{_drift_cl}">{f(_drift*100,2,True)}%</b>'
         f'  ({f(_drift,4,True)} BTC  ≈ {f(_drift*spot,0,True)}$)')}
    {row("Seuil rebal. (IV-adj)",
         f'<span class="neu">{f(hedge_thr_pct,1)}% = {f(hedge_thr_btc,4)} BTC</span>'
         f'  <span class="{"warn" if _drift_abs>hedge_thr_btc else "ok"}" style="font-size:0.8rem">'
         f'{"⚠️ REBALANCER" if _drift_abs>hedge_thr_btc else "✅ OK"}</span>')}
    {row("Dernier rebal.", last_reb_html)}
  </table>
  {drift_bar_html}
</div>"""

# ── Card PnL global ────────────────────────────────────────────────────────────
def _pnl_global_card() -> str:
    # pnl_opt_total calculé depuis positions_detail (même source que les cartes attribution)
    # pour rester cohérent avec positions_list après expiry/roll
    # _match_live(p) (pas pd_map) : apparie chaque lot à son détail → pas de double-comptage en ré-entrée
    pnl_opt_total = sum(float(_match_live(p).get("pnl_option_usd", 0)) for p in positions_list)
    funding       = float(s.get("funding_pnl_usd", 0))
    total_prem    = sum(float(p.get("entry_price",0)) * float(p.get("entry_spot",spot)) * float(p.get("contracts",1)) for p in positions_list)

    # Décomposition option : mid/mid total + coût B/A entrée total
    # Même formule que pnl_monitor : (mark entrée × spot entrée − mark actuel × spot actuel) × contracts
    midmid_total = sum(
        (float(p.get("entry_mark_price", p.get("entry_price", 0))) * float(p.get("entry_spot", spot)) -
         float(_match_live(p).get(
             "current_price_btc",
             float(p.get("entry_mark_price", p.get("entry_price", 0)))  # fallback: 0 PnL si pas encore de données live
         )) * spot) * float(p.get("contracts", 1))
        for p in positions_list
    )
    ba_entry_total = sum(
        -(float(p.get("entry_mark_price", p.get("entry_price", 0))) - float(p.get("entry_price", 0)))
        * float(p.get("entry_spot", spot)) * float(p.get("contracts", 1))
        for p in positions_list
    )

    hedge_mtm      = pnl_hedge_usd - realized_hedge   # flottant sur le short restant
    pnl_total_open = pnl_opt_total + hedge_mtm + funding
    realized_total = pnl_hist_total + realized_hedge   # options clôturées + rebalancements hedge

    return f"""<div class="card total-card">
  <h2>PnL global ouvert</h2>
  <table>
    <tr><td colspan="2" style="color:#8b949e;font-size:0.75rem;padding-bottom:4px">OPTIONS (positions ouvertes)</td></tr>
    <tr>
      <td class="label" style="font-size:0.82rem">Mid / mid total
        <span style="color:#484f58;font-size:0.72rem;display:block">(mark entrée × spot entrée − mark actuel × spot) × contrats</span>
      </td>
      <td class="val {color(midmid_total)}">{f(midmid_total,0,True)}$</td>
    </tr>
    <tr>
      <td class="label" style="font-size:0.82rem">Coût B/A entrée total
        <span style="color:#484f58;font-size:0.72rem;display:block">vendu au bid, non au mid</span>
      </td>
      <td class="val {color(ba_entry_total)}">{f(ba_entry_total,0,True)}$</td>
    </tr>
    <tr style="border-top:1px solid #21262d">
      <td class="label"><b>= Option latent total</b>
        <span style="color:#484f58;font-size:0.72rem;display:block">(bid entrée × spot entrée − mark actuel × spot) × contrats</span>
      </td>
      <td class="val {color(pnl_opt_total)}"><b>{f(pnl_opt_total,0,True)}$</b></td>
    </tr>
    <tr><td colspan="2" style="color:#8b949e;font-size:0.75rem;padding-top:10px;padding-bottom:4px">HEDGE BTC-PERP (flottant)</td></tr>
    <tr>
      <td class="label" style="font-size:0.82rem">MtM short restant
        <span style="color:#484f58;font-size:0.72rem;display:block">{f(hedge_qty,4)} BTC @ VWAP ${f(hedge_avg,0)} vs spot ${f(spot,0)}</span>
      </td>
      <td class="val {color(hedge_mtm)}">{f(hedge_mtm,0,True)}$</td>
    </tr>
    {srow("Funding perp", funding)}
    <tr><td colspan="2"><hr style="border-color:#30363d;margin:6px 0"></td></tr>
    <tr>
      <td class="label"><b>LATENT TOTAL</b>
        <span style="color:#484f58;font-size:0.72rem;display:block">option + hedge MtM + funding</span>
      </td>
      <td class="val {color(pnl_total_open)} big">{f(pnl_total_open,0,True)}$</td>
    </tr>
    <tr><td colspan="2" style="color:#8b949e;font-size:0.75rem;padding-top:12px;padding-bottom:4px">RÉALISÉ CUMULÉ</td></tr>
    {row("Options clôturées", f'<span class="{color(pnl_hist_total)}">{f(pnl_hist_total,0,True)}$</span>' + (f'  <span style="color:#8b949e;font-size:0.75rem">({len(hist)} pos.)</span>' if hist else ''))}
    {row("Rebalancements hedge", f'<span class="{color(realized_hedge)}">{f(realized_hedge,0,True)}$</span><span style="color:#484f58;font-size:0.72rem"> rachats/ventes partiels BTC-PERP</span>')}
    <tr style="border-top:1px solid #30363d;font-weight:600">
      <td class="label"><b>TOTAL RÉALISÉ</b></td>
      <td class="val {color(realized_total)}">{f(realized_total,0,True)}$</td>
    </tr>
    <tr><td colspan="2"><hr style="border-color:#30363d;margin:6px 0"></td></tr>
    <tr>
      <td class="label"><b>TOTAL STRATÉGIE</b>
        <span style="color:#484f58;font-size:0.72rem;display:block">réalisé + latent</span>
      </td>
      <td class="val {color(realized_total + pnl_total_open)}">{f(realized_total + pnl_total_open,0,True)}$</td>
    </tr>
  </table>
</div>"""

# ── Tableau hedge history ──────────────────────────────────────────────────────
HEDGE_HISTORY_DAYS = 3   # exécutions affichées par défaut (le reste est repliable)

def _hedge_history_card() -> str:
    hh = hedge_data.get("history", [])
    if not hh:
        return ""
    cutoff = datetime.now(timezone.utc) - timedelta(days=HEDGE_HISTORY_DAYS)

    def _row(h, hidden=False):
        side    = h.get("side","?")
        side_cl = "neg" if side == "SELL" else "pos"
        rpnl    = h.get("realized_pnl_usd")
        rpnl_h  = (f'<span class="{color(rpnl)}">{f(rpnl,0,True)}$</span>'
                   if rpnl is not None else '<span style="color:#484f58">—</span>')
        cls = ' class="hh-old" style="display:none"' if hidden else ""
        return f"""<tr{cls}>
      <td class="left muted" style="font-size:0.78rem">{to_ny(h.get("ts",""))}</td>
      <td class="{side_cl}"><b>{side}</b></td>
      <td class="{side_cl}">{f(h.get("qty",0),5,True)} BTC</td>
      <td>${f(h.get("spot",0),0)}</td>
      <td class="muted">{f(h.get("qty_before",0),5)} BTC</td>
      <td><b>{f(h.get("qty_after",0),5)} BTC</b></td>
      <td class="muted">${f(h.get("vwap_before",0),2)}</td>
      <td><b>${f(h.get("vwap_after",0),2)}</b></td>
      <td>{rpnl_h}</td>
      <td class="muted" style="font-size:0.75rem">{h.get("note","")}</td>
    </tr>"""

    # Tri antichrono, split récent (≤ 3j) / ancien
    hh_sorted = sorted(hh, key=lambda h: (_parse_ts(h.get("ts")) or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    recent = [h for h in hh_sorted if (_parse_ts(h.get("ts")) or cutoff) >= cutoff]
    older  = [h for h in hh_sorted if h not in recent]
    # Toujours montrer au moins les 3 dernières exécutions même si > 3j
    if len(recent) < 3:
        promote = older[:3 - len(recent)]
        recent += promote
        older   = older[len(promote):]

    rows = "".join(_row(h) for h in recent) + "".join(_row(h, hidden=True) for h in older)
    toggle = ""
    if older:
        toggle = f"""<div style="margin-top:8px">
    <a href="#" id="hh-toggle" style="color:#58a6ff;font-size:0.8rem;text-decoration:none"
       onclick="event.preventDefault();var o=document.querySelectorAll('.hh-old');var show=o[0].style.display==='none';o.forEach(r=>r.style.display=show?'':'none');this.textContent=show?'▲ Réduire ({HEDGE_HISTORY_DAYS} derniers jours)':'▼ Voir tout ({len(older)} exécutions plus anciennes)';">▼ Voir tout ({len(older)} exécutions plus anciennes)</a>
  </div>"""

    return f"""<div class="card full">
  <h2>🔄 Historique hedge — {len(recent)} récente(s) / {len(hh)} au total  <span style="font-weight:400;color:#484f58">VWAP actuel ${f(hedge_avg,2)} · Qty {f(hedge_qty,5)} BTC</span></h2>
  <div style="overflow-x:auto">
  <table class="tbl">
    <tr>
      <th style="text-align:left">Date / Heure</th>
      <th>Côté</th><th>Ordre</th><th>Spot</th>
      <th>Avant</th><th>Après</th>
      <th>VWAP avant</th><th>VWAP après</th>
      <th>PnL réalisé</th><th style="text-align:left">Note</th>
    </tr>
    {rows}
  </table>
  </div>
  {toggle}
</div>"""

# ── Opportunités d'entrée ─────────────────────────────────────────────────────
def _scan_entry_card() -> str:
    if not scan_entry:
        return ""
    ctx   = scan_entry.get("market_context", {})
    top7  = scan_entry.get("top7", scan_entry.get("top5", []))  # compat ancien format
    ts_se = to_ny(scan_entry.get("ts", "—"))
    sig   = ctx.get("signal_ok", False)
    sig_cl = "pos" if sig else "neg"

    iv_rank_pct = float(ctx.get("iv_rank", 0)) * 100
    iv_rank_cl  = "pos" if iv_rank_pct >= 60 else ("warn" if iv_rank_pct >= 30 else "neg")

    # Légende statut
    STATUS_LABEL = {
        "eligible":     ("✅", "ok",   "Éligible"),
        "held":         ("📌", "neu",  "En position"),
        "held_reentry": ("🔁", "warn", "En position — re-entrée possible"),
        "filtered":     ("🚫", "neg",  "Filtré (trop proche d'une pos. tenue)"),
    }

    rows = ""
    for i, c in enumerate(top7):
        sc      = float(c.get("score", 0))
        status  = c.get("status", "eligible")
        held_sc = c.get("held_entry_score")
        s_icon, s_cl, s_lbl = STATUS_LABEL.get(status, ("", "neu", status))
        sc_cl   = "pos" if sc >= 0.45 else ("warn" if sc >= 0.35 else "neg")
        ba      = float(c.get("ba_pct", 0))
        ba_cl   = "neg" if ba > 50 else ("warn" if ba > 12 else "ok")
        ivhv    = float(c.get("iv_hv_ratio", 0))
        ivhv_cl = "pos" if ivhv >= 1.10 else "neg"
        # Score pénalisé + score brut (avant pénalité gamma) si différent
        score_raw = float(c.get("score_raw", sc))
        score_cell = f'{f(sc,3)}'
        if abs(score_raw - sc) > 0.001:
            score_cell += f' <span class="muted" style="font-size:0.75rem" title="Score brut avant pénalité gamma">({f(score_raw,3)})</span>'
        # Pour re-entrée : afficher delta score vs score initial
        if held_sc is not None:
            delta_sc = sc - float(held_sc)
            dc = "pos" if delta_sc > 0.05 else ("warn" if delta_sc > 0 else "neg")
            score_cell += f' <span class="{dc}" style="font-size:0.75rem">({f(delta_sc,3,True)})</span>'
        row_style = "opacity:0.5" if status == "filtered" else ""
        gpts    = float(c.get("gamma_pts", 0))
        gpts_cl = "neg" if gpts > 5 else ("warn" if gpts > 2.5 else "ok")
        skew    = float(c.get("skew_pct", 0))
        s_skew  = float(c.get("s_skew", 0))
        skew_cl = "pos" if s_skew >= 0.5 else ("warn" if s_skew >= 0.25 else "neu")
        zsc     = float(c.get("z_score", 0))
        zsc_cl  = "pos" if zsc >= 1.0 else ("warn" if zsc >= 0.6 else "neg")
        rows += f"""<tr {"class='hl'" if (i==0 and status=="eligible") else ""} style="{row_style}">
      <td class="left"><span title="{s_lbl}">{s_icon}</span> <b>{c.get("instrument_name","—")}</b></td>
      <td class="{sc_cl}" style="font-weight:700">{score_cell}</td>
      <td>${int(c.get("strike",0)):,}</td>
      <td>{f(c.get("tte_days",0),1)}j</td>
      <td>{f(c.get("delta",0),3)}</td>
      <td class="{gpts_cl}">{f(gpts,2)}</td>
      <td>{f(c.get("mark_iv",0),1)}%</td>
      <td class="{ivhv_cl}">{f(ivhv,2)}x</td>
      <td class="{skew_cl}">{f(skew,1,True)}%</td>
      <td class="{zsc_cl}">{f(zsc,2)}</td>
      <td>{f(c.get("yield_ann_pct",0),1)}%/an</td>
      <td class="{ba_cl}">{f(ba,1)}%</td>
      <td>${f(float(c.get("premium_usd", float(c.get("mark_price",0)) * _se_spot)), 0)}</td>
    </tr>"""

    return f"""<div class="card full">
  <h2>Opportunites d\'entree — scan du {ts_se}</h2>
  <div style="display:flex;gap:20px;margin-bottom:12px;font-size:0.82rem;flex-wrap:wrap;align-items:center">
    <span>HV 10j <b>{f(ctx.get("hv_10d",0),1)}%</b></span>
    <span title="½ HV10j + ½ HV30j — référence du score IV/HV">HV blend <b>{f(ctx.get("hv_blend",0),1)}%</b></span>
    <span>DVOL <b>{f(ctx.get("curr_iv",0),1)}%</b></span>
    <span>Rang DVOL 30j <b class="{iv_rank_cl}">{f(iv_rank_pct,0)}%</b></span>
    <span>Régime <b>{ctx.get("regime","—")}</b></span>
    <span class="{sig_cl}"><b>{"Signal OK — DVOL ≥ 35%" if sig else "Signal inactif — DVOL < 35%"}</b></span>
  </div>
  <div style="overflow-x:auto">
  <table class="tbl">
    <tr>
      <th style="text-align:left">Instrument</th>
      <th>Score <span style="font-weight:400;font-size:0.75rem;color:#484f58">(Δ vs entrée)</span></th>
      <th>Strike</th><th>TTE</th><th>Delta</th>
      <th title="Δ delta points par 1% move du spot (par contrat)">Γ pts/1%</th>
      <th>IV option</th><th>IV/HV <span style="font-weight:400;color:#484f58">(≥1.10)</span></th>
      <th title="Richesse de l'IV bid vs ATM de la même échéance (norme : 60% → s_skew = 1.0)">Skew vs ATM</th>
      <th title="Distance au strike en écarts-types de vol réalisée : OTM% / (HV×√TTE)">z</th>
      <th>Yield ann.</th><th>B/A <span style="font-weight:400;color:#484f58">(≤50%)</span></th><th title="Prime au bid pour 1 BTC (1 contrat Deribit) — plancher 150$">Prime bid / 1 BTC</th>
    </tr>
    {rows if rows else '<tr><td colspan="13" class="muted" style="text-align:center">Aucun candidat</td></tr>'}
  </table>
  </div>
  <div style="margin-top:10px;font-size:0.78rem;color:#8b949e">
    ✅ Éligible · 📌 En position · 🔁 Re-entrée possible (score +0.05) · 🚫 Filtré (même expiry, delta trop proche) ·
    <b>Diversification</b> : espacement delta ≥ 0.08 entre positions de même expiry · 1 entrée max par cycle
  </div>
  <div style="margin-top:8px;font-size:0.78rem;color:#8b949e;border-top:1px solid #21262d;padding-top:8px">
    Score = [0.30·s<sub>iv/hv</sub> + 0.25·s<sub>yield</sub> + 0.45·s<sub>skew</sub>] × pénalité gamma · chaque composante clampée dans [0,1] :
    <br>&nbsp;&nbsp;s<sub>iv/hv</sub> = clamp((IV<sub>bid</sub>/HV<sub>blend</sub> − 1) / <b>1.50</b>) ·
    s<sub>yield</sub> = clamp(yield<sub>ann</sub> × z / <b>0.30</b>), z = OTM% / (HV<sub>blend</sub>×√TTE) ·
    s<sub>skew</sub> = clamp((IV<sub>bid</sub>/IV<sub>ATM</sub> − 1) / <b>0.60</b>)
    <br>&nbsp;&nbsp;HV<sub>blend</sub> = ½HV10j + ½HV30j · les dénominateurs (1.50 / 0.30 / 0.60) sont les normalisations : la composante atteint 1.0 à cette valeur · Score brut (gris) = avant pénalité gamma ·
    <b>Seuils</b> : score ≥ 0.45 · prime ≥ 150$/BTC · B/A ≤ 50% (anti-illiquidité) · DVOL ≥ 35% ·
    <b>Sizing</b> : round(score<sup>1.5</sup> × (0.5 + 0.5 × rang DVOL 30j), 1) BTC · max 5 BTC
  </div>
</div>"""

# ── HTML ───────────────────────────────────────────────────────────────────────
title    = f"VRP Monitor — {n_instruments} position(s)" if positions_list else "VRP Monitor"
_spot_cl = "pos" if (_delta_spot or 0) >= 0 else "neg"
_pnl_cl  = "pos" if pnl_open_latent >= 0 else "neg"
_dpnl_cl = ("pos" if (_delta_pnl or 0) >= 0 else "neg") if _delta_pnl is not None else "neu"
_tte_min = min((float(pd_map.get(p.get("instrument_name",""),{}).get("tte_days",99)) for p in positions_list), default=99)
_tte_cl  = "warn" if _tte_min <= 1 else "neu"

_spot_delta_html = (
    f'<span class="chip-delta {_spot_cl}">{f(_delta_spot,0,True)}$ ({f(_delta_spot_pct,2,True)}%) vs snapshot préc.</span>'
) if _delta_spot is not None else '<span class="chip-delta neu">— premier snapshot</span>'

def _move_line(label, move_abs, move_pct):
    if move_abs is None:
        return ""
    cl = "pos" if move_pct >= 0 else "neg"
    return f'<span class="chip-delta {cl}">{label} {f(move_pct,2,True)}% ({f(move_abs,0,True)}$)</span>'

_move4h_html = _move_line("4h", _move4h_abs, _move4h_pct)
_move1d_html = _move_line("1j", _move1d_abs, _move1d_pct)

# ── DVOL / HV moves ────────────────────────────────────────────────────────────
# Valeurs DVOL/HV actuelles depuis scan_entry.json (même source que scan card)
_ctx_mc    = scan_entry.get("market_context", {}) if scan_entry else {}
_dvol_curr = float(_ctx_mc.get("curr_iv", 0))
_hv_curr   = float(_ctx_mc.get("hv_10d", 0))

# Move 1j DVOL/HV : lus directement depuis scan_entry (calculés par greeks_hedge depuis l'API)
_dvol_1d_chg = _ctx_mc.get("dvol_1d_chg")   # pp absolus (ex: -1.4)
_hv_1d_chg   = _ctx_mc.get("hv_1d_chg")

def _vol_chip_delta(move_abs):
    if move_abs is None:
        return ""
    cl = "pos" if move_abs >= 0 else "neg"
    sign = "+" if move_abs >= 0 else ""
    return f'<span class="chip-delta {cl}">1j {sign}{move_abs:.1f}pp</span>'

_dvol_1d_html = _vol_chip_delta(_dvol_1d_chg)
_hv_1d_html   = _vol_chip_delta(_hv_1d_chg)

# ── Circuit breaker (gradué : allègement −5%/1j ou −6%/3j → trim 30% ; fermeture −10%/+12pts) ──
_cb_risk_off = bool(_ctx_mc.get("risk_off", pos_raw.get("risk_off", False)))
_cb_reduced  = bool(_ctx_mc.get("cb_reduced", pos_raw.get("cb_reduced", False)))
_cb_move_3d  = _ctx_mc.get("cb_move_3d")
_cb_move_1d  = _ctx_mc.get("cb_move_1d")
_cb_dvol_3d  = _ctx_mc.get("cb_dvol_3d")

def _cb_chip() -> str:
    if _cb_risk_off:
        info = pos_raw.get("risk_off_info", {})
        since = to_ny(info.get("ts", ""))[:16] if info.get("ts") else "—"
        return f"""<div class="chip" style="border-color:#f85149">
    <span class="chip-label" style="color:#f85149">⛔ Circuit breaker</span>
    <span class="chip-value" style="color:#f85149">RISK-OFF</span>
    <span class="chip-delta neu">depuis {since}</span>
    <span class="chip-delta neu">re-entrée : HV5 &lt; HV10 et |move 3j| &lt; 4%</span>
  </div>"""
    parts = []
    # Palier 1 (allègement) : chute 1j < −5% OU 3j < −6%
    if _cb_move_1d is not None:
        mv1 = float(_cb_move_1d)
        cl = "neg" if mv1 < -5 else ("warn" if mv1 < -3.5 else "ok")
        parts.append(f'<span class="chip-delta {cl}">move 1j {mv1:+.1f}% / −5%</span>')
    if _cb_move_3d is not None:
        mv = float(_cb_move_3d)
        # seuils : −6% (allègement) puis −10% (fermeture totale)
        cl = "neg" if mv < -10 else ("warn" if mv < -6 else "ok")
        parts.append(f'<span class="chip-delta {cl}">move 3j {mv:+.1f}% / −6% · −10%</span>')
    if _cb_dvol_3d is not None:
        dv = float(_cb_dvol_3d)
        cl = "neg" if dv > 9 else ("warn" if dv > 6 else "ok")
        parts.append(f'<span class="chip-delta {cl}">DVOL 3j {dv:+.1f}pt / +12pt</span>')
    if not parts:
        return ""
    if _cb_reduced:
        head = ('<span class="chip-value" style="font-size:0.9rem;color:#d29922">ALLÉGÉ 30%</span>'
                '<span class="chip-delta neu">reprise : |move 3j| &lt; 3%</span>')
    else:
        head = '<span class="chip-value ok" style="font-size:0.9rem">armé</span>'
    return f"""<div class="chip"{' style="border-color:#d2992244"' if _cb_reduced else ''}>
    <span class="chip-label">Circuit breaker</span>
    {head}
    {''.join(parts)}
  </div>"""

_cb_chip_html = _cb_chip()

# ── PnL 1j ─────────────────────────────────────────────────────────────────────
def _pnl_move_1d():
    if not pnl_history:
        return None
    now_utc = datetime.now(timezone.utc)
    target_dt = now_utc - timedelta(hours=24)
    best, best_diff = None, None
    for p in pnl_history[:-1]:
        dt = _parse_ts(p.get("ts"))
        if dt is None:
            continue
        diff = abs((dt - target_dt).total_seconds())
        if best_diff is None or diff < best_diff:
            best, best_diff = p, diff
    if best is None or best_diff is None or best_diff > 3 * 3600:
        return None
    ref = float(best.get("pnl_total") or 0)
    return _curr_pnl - ref

_pnl_1d = _pnl_move_1d()
_pnl_1d_html = (
    f'<span class="chip-delta {"pos" if (_pnl_1d or 0) >= 0 else "neg"}">{f(_pnl_1d, 0, True)}$ sur 1j</span>'
) if _pnl_1d is not None else ""

# ── Dernière transaction (hedge rebalancement, roll, nouvelle position) ─────────
def _last_tx_html() -> str:
    events = []

    # Rebalancements hedge
    for h in hedge_data.get("history", []):
        note = h.get("note", "")
        if "entree opportuniste" in note or "hedge initial" in note:
            if "entree opportuniste" in note:
                label = f"Nouvelle position ({note.replace('entree opportuniste ', '')})"
                cat = "new"
            else:
                label = f"Ouverture hedge ({note.replace('hedge initial ', '')})"
                cat = "new"
        else:
            side = h.get("side", "?")
            qty  = abs(float(h.get("qty", 0)))
            label = f"Rebal. hedge — {side} {qty:.4f} BTC-PERP"
            cat = "hedge"
        events.append({"ts": h.get("ts", ""), "label": label, "cat": cat})

    # Rolls (history des positions clôturées)
    for h in hist:
        events.append({"ts": h.get("exit_ts", ""), "label": f"Roll — {h.get('instrument_name','?')}", "cat": "roll"})

    # Ouvertures de positions courantes (entrée)
    for p in positions_list:
        events.append({"ts": p.get("entry_ts", ""), "label": f"Ouverture — {p.get('instrument_name','?')}", "cat": "new"})

    # Trier par timestamp décroissant, prendre le plus récent
    def _ts_key(e):
        dt = _parse_ts(e["ts"])
        return dt.timestamp() if dt else 0

    events = [e for e in events if e["ts"]]
    if not events:
        return '<span class="chip-delta neu">—</span>'

    last = max(events, key=_ts_key)
    cat_cl = {"hedge": "warn", "roll": "neg", "new": "pos"}.get(last["cat"], "neu")
    return f'<span class="chip-delta {cat_cl}">{to_ny(last["ts"])[:16]}</span><span class="chip-delta neu" style="margin-top:1px">{last["label"]}</span>'

_last_tx = _last_tx_html()

_pnl_delta_html = (
    f'<span class="chip-delta {_dpnl_cl}">{f(_delta_pnl,0,True)}$ vs snapshot préc.</span>'
) if _delta_pnl is not None else '<span class="chip-delta neu">— premier snapshot</span>'

html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<title>{title}</title>
<style>{CSS}</style>
</head>
<body>
"""

if no_position:
    html += "<h1>Aucune position ouverte.</h1></body></html>"
else:
    html += f"""
<h1>📊 VRP Monitor — {n_instruments} position(s) ouverte(s){_lots_suffix}
  <a href="backtest.html" style="float:right;font-size:0.6em;font-weight:500;color:#58a6ff;text-decoration:none;border:1px solid #30363d;border-radius:6px;padding:5px 12px;background:#161b22">📊 Backtests &amp; Skew →</a></h1>
<div class="subtitle">Données : {ts} · Généré : {generated} · ↻ auto-refresh 5min</div>

<div class="header-bar">
  <div class="chip">
    <span class="chip-label">Spot BTC</span>
    <span class="chip-value">${f(_curr_spot,0)}</span>
    {_spot_delta_html}
    {_move4h_html}
    {_move1d_html}
  </div>
  <div class="chip" title="Latent = MtM des positions ouvertes (option latente + hedge flottant). Réalisé = options clôturées + rebalancements hedge déjà encaissés. Total = somme.">
    <span class="chip-label">PnL ouvert (latent)</span>
    <span class="chip-value {_pnl_cl}">{f(pnl_open_latent,0,True)}$</span>
    {_pnl_delta_html}
    {_pnl_1d_html}
    <span class="chip-delta {color(pnl_realized_total)}">Réalisé cumulé {f(pnl_realized_total,0,True)}$</span>
    <span class="chip-delta {color(pnl_strategy_total)}" style="border-top:1px solid #21262d;padding-top:2px;margin-top:2px">Total stratégie <b>{f(pnl_strategy_total,0,True)}$</b></span>
  </div>
  <div class="chip">
    <span class="chip-label">DVOL (index)</span>
    <span class="chip-value">{"—" if _dvol_curr == 0 else f"{_dvol_curr:.1f}%"}</span>
    {_dvol_1d_html}
  </div>
  <div class="chip">
    <span class="chip-label">HV 10j</span>
    <span class="chip-value">{"—" if _hv_curr == 0 else f"{_hv_curr:.1f}%"}</span>
    {_hv_1d_html}
  </div>
  {_cb_chip_html}
  <div class="chip">
    <span class="chip-label">Positions</span>
    <span class="chip-value">{n_instruments}{f'<span style="font-size:0.7rem;color:#8b949e"> · {n_lots} lots</span>' if n_lots > n_instruments else ''}</span>
    <span class="chip-delta neu">Nominal <b>{f(sum(float(p.get("contracts",1)) for p in positions_list),1)} BTC</b> / {f(5.0,1)} BTC max</span>
    <span class="chip-delta neu">TTE min <span class="{_tte_cl}">{f(_tte_min,2)}j</span></span>
  </div>
  <div class="chip">
    <span class="chip-label">Net delta</span>
    <span class="chip-value {_drift_cl}">{f(_drift*100,2,True)}%  ≈  {f(_drift*spot,0,True)}$</span>
    <span class="chip-delta neu">Seuil rebal. {f(hedge_thr_pct,1)}% · <span class="{"neg" if _drift_abs>hedge_thr_btc else "ok"}">{"REBALANCER" if _drift_abs>hedge_thr_btc else "OK"}</span></span>
  </div>
  <div class="chip" style="min-width:220px">
    <span class="chip-label">Derniere transaction</span>
    {_last_tx}
  </div>
</div>

<div class="grid">
{_positions_table()}
</div>

<div class="grid" style="margin-top:16px">
{_greeks_card()}
{_pnl_global_card()}
</div>

<div class="section-title">Attribution PnL par position</div>
<div class="grid-3">
"""

    for _lots in _group_positions():
        html += _attr_card(_lots)  # agrège les ré-entrées (lots du même instrument)

    html += "</div>\n<div class=\"grid\" style=\"margin-top:16px\">\n"
    html += _scan_entry_card()
    html += _hedge_history_card()

    # Historique des clôtures — n'affiche que les 7 derniers jours, le reste repliable ;
    # chaque ligne est cliquable pour déplier le détail complet de la position clôturée.
    if hist:
        CLOSED_HISTORY_DAYS = 7
        REASON_LABEL = {
            "circuit_breaker": "🛑 Circuit breaker (fermeture totale)",
            "cb_tier1_trim":   "⚠️ CB palier 1 (allègement)",
            "expiry":          "⏱️ Expiration",
            "roll":            "🔁 Roll",
            "expired":         "⏱️ Expiration",
        }
        c_cutoff = datetime.now(timezone.utc) - timedelta(days=CLOSED_HISTORY_DAYS)

        def _closed_detail(h) -> str:
            entry_p  = float(h.get("entry_price", 0))
            exit_p   = float(h.get("exit_price", 0))
            entry_s  = float(h.get("entry_spot", 0))
            exit_s   = float(h.get("exit_spot", 0))
            ctr      = float(h.get("contracts", 1))
            strike   = h.get("strike")
            pnl_btc  = h.get("pnl_btc")
            items = [
                ("Contrats", f"{f(ctr,4)}"),
                ("Strike", f"${f(strike,0)}" if strike else "—"),
                ("Prime entrée (bid)", f"{f(entry_p,5)} BTC  (${f(entry_p*entry_s,0)})"),
                ("Prix sortie (ask)", f"{f(exit_p,5)} BTC  (${f(exit_p*exit_s,0)})"),
                ("Spot entrée", f"${f(entry_s,0)}"),
                ("Spot sortie", f"${f(exit_s,0)}"),
                ("Entrée", str(h.get("entry_ts",""))[:16].replace("T"," ")),
                ("Sortie", str(h.get("exit_ts",""))[:16].replace("T"," ")),
                ("PnL (BTC)", f"{f(pnl_btc,6,True)}" if pnl_btc is not None else "—"),
                ("PnL (USD)", f"{f(float(h.get('pnl_usd',0)),2,True)}$"),
            ]
            cells = "".join(
                f'<div style="display:flex;justify-content:space-between;gap:12px;padding:2px 0">'
                f'<span style="color:#8b949e">{k}</span><span>{v}</span></div>'
                for k, v in items)
            return (f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));'
                    f'gap:4px 24px;padding:8px 12px;font-size:0.8rem">{cells}</div>')

        def _closed_row(h, idx, hidden=False):
            pnl_h  = float(h.get("pnl_usd", 0))
            reason = h.get("exit_reason", "—")
            r_lbl  = REASON_LABEL.get(reason, reason)
            cls    = ' class="ch-old" style="display:none"' if hidden else ""
            dcls   = ' class="ch-old"' if hidden else ""
            return f"""<tr{cls} style="cursor:pointer" onclick="var d=document.getElementById('cd-{idx}');d.style.display=d.style.display==='none'?'table-row':'none';">
          <td class="left">{h.get("instrument_name","?")}</td>
          <td class="muted">{str(h.get("entry_ts",""))[:10]}</td>
          <td class="muted">{str(h.get("exit_ts",""))[:10]}</td>
          <td class="{color(pnl_h)}"><b>{f(pnl_h,0,True)}$</b></td>
          <td class="muted">{r_lbl} <span style="color:#58a6ff;font-size:0.72rem">▾ détail</span></td>
        </tr>
        <tr id="cd-{idx}"{dcls} style="display:none"><td colspan="5" style="background:#0d1117;padding:0">{_closed_detail(h)}</td></tr>"""

        h_sorted = sorted(hist, key=lambda h: (_parse_ts(h.get("exit_ts")) or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
        recent = [h for h in h_sorted if (_parse_ts(h.get("exit_ts")) or c_cutoff) >= c_cutoff]
        older  = [h for h in h_sorted if h not in recent]

        h_rows = "".join(_closed_row(h, i) for i, h in enumerate(recent))
        h_rows += "".join(_closed_row(h, len(recent)+i, hidden=True) for i, h in enumerate(older))

        toggle = ""
        if older:
            older_pnl = sum(float(h.get("pnl_usd",0)) for h in older)
            toggle = f"""<div style="margin-top:8px">
    <a href="#" id="ch-toggle" style="color:#58a6ff;font-size:0.8rem;text-decoration:none"
       onclick="event.preventDefault();var o=document.querySelectorAll('.ch-old');var show=o[0].style.display==='none';o.forEach(r=>{{if(r.id&&r.id.indexOf('cd-')===0){{r.style.display='none';}}else{{r.style.display=show?'':'none';}}}});this.textContent=show?'▲ Réduire (7 derniers jours)':'▼ Voir tout ({len(older)} clôture(s) plus anciennes · {f(older_pnl,0,True)}$)';">▼ Voir tout ({len(older)} clôture(s) plus anciennes · {f(older_pnl,0,True)}$)</a>
  </div>"""

        html += f"""<div class="card full">
  <h2>📈 Positions clôturées — {len(recent)} sur 7j / {len(hist)} au total</h2>
  <table class="tbl">
    <tr><th style="text-align:left">Instrument</th><th style="text-align:left">Entrée</th>
        <th style="text-align:left">Sortie</th><th>PnL</th><th style="text-align:left">Raison</th></tr>
    {h_rows}
    <tr style="border-top:1px solid #30363d;font-weight:600">
      <td colspan="3">TOTAL RÉALISÉ ({len(hist)} clôture(s))</td>
      <td class="{color(pnl_hist_total)}">{f(pnl_hist_total,0,True)}$</td>
      <td></td>
    </tr>
  </table>
  {toggle}
</div>"""

    html += "</div>\n"  # ferme la grille

# ── Graphiques ────────────────────────────────────────────────────────────────
if pnl_history:
    import json as _json

    labels       = [to_ny(p["ts"])[:16]     for p in pnl_history]
    delta_data   = [p.get("delta_pct",0)    for p in pnl_history]
    net_d_data   = [p.get("net_delta_pct",0)for p in pnl_history]
    gamma_data   = [p.get("gamma_pts",0)    for p in pnl_history]
    iv_data      = [p.get("iv_pct",0)       for p in pnl_history]
    pnl_opt      = [p.get("pnl_option",0)   for p in pnl_history]
    pnl_hdg      = [p.get("pnl_hedge",0)    for p in pnl_history]
    # pnl_total des snapshots = latent options ouvertes + hedge (flottant+réalisé) + funding.
    # Il EXCLUT le réalisé des options clôturées (history) → diverge du header/card
    # « Total stratégie » dès qu'une position est clôturée (expiry, CB). On le ré-aligne en
    # ajoutant, à chaque snapshot, le réalisé-options cumulé jusqu'à son timestamp.
    _closed_opts = sorted(
        ((_parse_ts(h.get("exit_ts")), float(h.get("pnl_usd", 0))) for h in hist),
        key=lambda x: (x[0] or datetime.min.replace(tzinfo=timezone.utc)))
    def _realized_opt_upto(ts_dt):
        if ts_dt is None:
            return 0.0
        return sum(p for (t, p) in _closed_opts if t is not None and t <= ts_dt)
    pnl_tot      = [round(float(p.get("pnl_total", 0)) + _realized_opt_upto(_parse_ts(p.get("ts"))), 2)
                    for p in pnl_history]
    spot_data    = [p.get("spot",0)         for p in pnl_history]
    n_pos_data   = [p.get("n_positions",1)  for p in pnl_history]

    dvol_data    = [p.get("dvol")           for p in pnl_history]
    hv5_data     = [p.get("hv_5d")          for p in pnl_history]
    hv10_data    = [p.get("hv_10d")         for p in pnl_history]
    hv30_data    = [p.get("hv_30d")         for p in pnl_history]

    # ── Seuils circuit breaker (référence = snapshot le plus proche de 72h avant)
    # Spot : ±10% vs spot d'il y a 3j · DVOL : +12 pts vs DVOL d'il y a 3j
    _ts_parsed = [_parse_ts(p.get("ts")) for p in pnl_history]
    cb_spot_low, cb_spot_low_t1, cb_dvol_thr = [], [], []
    for _i, _dt in enumerate(_ts_parsed):
        _ref_idx = None
        if _dt is not None:
            _target = _dt - timedelta(hours=72)
            _bd = None
            for _j in range(_i):
                if _ts_parsed[_j] is None:
                    continue
                _diff = abs((_ts_parsed[_j] - _target).total_seconds())
                if _bd is None or _diff < _bd:
                    _bd, _ref_idx = _diff, _j
            if _bd is None or _bd > 12 * 3600:   # pas de référence dans ±12h
                _ref_idx = None
        if _ref_idx is not None:
            try:
                _ref_spot = float(spot_data[_ref_idx] or 0)
            except (TypeError, ValueError):
                _ref_spot = 0.0
            try:
                _ref_dvol = float(dvol_data[_ref_idx]) if dvol_data[_ref_idx] is not None else None
            except (TypeError, ValueError):
                _ref_dvol = None
            cb_spot_low.append(round(_ref_spot * 0.90, 0) if _ref_spot else None)     # −10% : fermeture
            cb_spot_low_t1.append(round(_ref_spot * 0.94, 0) if _ref_spot else None)  # −6% : allègement
            cb_dvol_thr.append(round(_ref_dvol + 12, 1) if _ref_dvol else None)
        else:
            cb_spot_low.append(None); cb_spot_low_t1.append(None); cb_dvol_thr.append(None)

    labels_js    = _json.dumps(labels)
    delta_js     = _json.dumps(delta_data)
    net_delta_js = _json.dumps(net_d_data)
    gamma_js     = _json.dumps(gamma_data)
    pnl_opt_js   = _json.dumps(pnl_opt)
    pnl_hdg_js   = _json.dumps(pnl_hdg)
    pnl_tot_js   = _json.dumps(pnl_tot)
    spot_js      = _json.dumps(spot_data)
    dvol_js      = _json.dumps(dvol_data)
    hv5_js       = _json.dumps(hv5_data)
    hv10_js      = _json.dumps(hv10_data)
    hv30_js      = _json.dumps(hv30_data)
    cb_low_js    = _json.dumps(cb_spot_low)
    cb_low_t1_js = _json.dumps(cb_spot_low_t1)
    cb_dvol_js   = _json.dumps(cb_dvol_thr)
    n_pts        = len(pnl_history)

    # Strikes — datasets pour le graphique dédié spot+strikes uniquement.
    # Dédoublonnage par valeur de strike : un même strike sur plusieurs maturités (ou
    # à la fois ouvert et clôturé) ne trace qu'une ligne. Les positions ouvertes passant
    # avant l'historique, la ligne pleine prime sur la pointillée en cas de collision.
    _strike_colors = ["#f85149", "#d29922", "#a371f7", "#58a6ff", "#3fb950"]
    _strike_datasets_spot = ""
    _seen_strikes = set()
    _ci = 0
    for _p in positions_list + hist:
        _strike = _p.get("strike")
        if not _strike:
            continue
        _key = round(float(_strike))
        if _key in _seen_strikes:
            continue
        _seen_strikes.add(_key)
        _is_closed = _p in hist
        _label = f"Strike {int(_strike):,}{' (clôturé)' if _is_closed else ''}"
        _col = _strike_colors[_ci % len(_strike_colors)]
        _ci += 1
        _dash = "[6,4]" if _is_closed else "[]"
        _data_js = _json.dumps([_strike] * n_pts)
        _strike_datasets_spot += f"""
    {{ label:{_json.dumps(_label)}, data:{_data_js}, borderColor:"{_col}", backgroundColor:"transparent",
      tension:0, pointRadius:0, borderWidth:1.5, borderDash:{_dash} }},"""

    html += f"""
<div class="chart-section">

<div class="chart-card">
  <h2>&#x1F4C8; Greeks — {n_pts} snapshots</h2>
  <div class="chart-wrap"><canvas id="chartGreeks"></canvas></div>
</div>

<div class="chart-card">
  <h2>&#x1F4B0; PnL — dans le temps</h2>
  <div class="chart-wrap"><canvas id="chartPnl"></canvas></div>
</div>

<div class="chart-card">
  <h2>&#x20BF; Spot BTC &amp; Strikes <span style="font-weight:400;color:#484f58;font-size:0.72rem">· CB : allègement −6% / fermeture −10% vs spot 3j</span></h2>
  <div class="chart-wrap"><canvas id="chartSpot"></canvas></div>
</div>

<div class="chart-card">
  <h2>&#x1F30A; Volatilité — DVOL vs HV réalisée <span style="font-weight:400;color:#484f58;font-size:0.72rem">· seuil CB = DVOL 3j + 12pts</span></h2>
  <div class="chart-wrap"><canvas id="chartVol"></canvas></div>
</div>

</div>

<script>
const LABELS = {labels_js};
const PT_R = LABELS.length > 50 ? 0 : 3;
const TT = {{ backgroundColor:"#161b22",borderColor:"#30363d",borderWidth:1,titleColor:"#e6edf3",bodyColor:"#8b949e" }};

new Chart(document.getElementById("chartGreeks"), {{
  type:"line",
  data:{{ labels:LABELS, datasets:[
    {{ label:"Delta pos (%)", data:{delta_js}, borderColor:"#58a6ff", backgroundColor:"rgba(88,166,255,0.07)",
      yAxisID:"yD", tension:0.3, pointRadius:PT_R, borderWidth:2, fill:true }},
    {{ label:"Delta net (%)", data:{net_delta_js}, borderColor:"#a371f7", backgroundColor:"transparent",
      yAxisID:"yD", tension:0.3, pointRadius:PT_R, borderWidth:2, borderDash:[4,2] }},
    {{ label:"Gamma (pts/1%)", data:{gamma_js}, borderColor:"#f85149", backgroundColor:"transparent",
      yAxisID:"yG", tension:0.3, pointRadius:PT_R, borderWidth:2, borderDash:[5,3] }},
    {{ label:"Spot BTC ($)", data:{spot_js}, borderColor:"rgba(210,153,34,0.7)", backgroundColor:"transparent",
      yAxisID:"yS", tension:0.3, pointRadius:0, borderWidth:2, borderDash:[2,4] }},
  ]}},
  options:{{ responsive:true, maintainAspectRatio:false,
    interaction:{{mode:"index",intersect:false}},
    plugins:{{ legend:{{labels:{{color:"#8b949e",font:{{size:11}}}}}}, tooltip:{{...TT}} }},
    scales:{{
      x:{{ticks:{{color:"#484f58",maxTicksLimit:14,maxRotation:30,font:{{size:10}}}},grid:{{color:"#21262d"}}}},
      yD:{{type:"linear",position:"left",ticks:{{color:"#58a6ff",font:{{size:10}},callback:v=>v.toFixed(1)+"%"}},grid:{{color:"#21262d"}}}},
      yG:{{type:"linear",position:"right",ticks:{{color:"#f85149",font:{{size:10}},callback:v=>v.toFixed(2)}},grid:{{drawOnChartArea:false}}}},
      yS:{{type:"linear",position:"right",ticks:{{color:"#d29922",font:{{size:10}},callback:v=>"$"+Math.round(v/1000)+"k"}},grid:{{drawOnChartArea:false}}}},
    }}
  }}
}});

new Chart(document.getElementById("chartPnl"), {{
  type:"line",
  data:{{ labels:LABELS, datasets:[
    {{ label:"PnL Option ($)", data:{pnl_opt_js}, borderColor:"#3fb950", backgroundColor:"rgba(63,185,80,0.07)",
      yAxisID:"yP", tension:0.3, pointRadius:PT_R, borderWidth:2, fill:true }},
    {{ label:"PnL Hedge ($)", data:{pnl_hdg_js}, borderColor:"#ff9800", backgroundColor:"transparent",
      yAxisID:"yP", tension:0.3, pointRadius:PT_R, borderWidth:2, borderDash:[5,3] }},
    {{ label:"PnL Total ($)", data:{pnl_tot_js}, borderColor:"#e6edf3", backgroundColor:"rgba(230,237,243,0.04)",
      yAxisID:"yP", tension:0.3, pointRadius:PT_R, borderWidth:2.5, fill:true }},
    {{ label:"Spot BTC ($)", data:{spot_js}, borderColor:"rgba(210,153,34,0.7)", backgroundColor:"transparent",
      yAxisID:"yS2", tension:0.3, pointRadius:0, borderWidth:2, borderDash:[2,4] }},
  ]}},
  options:{{ responsive:true, maintainAspectRatio:false,
    interaction:{{mode:"index",intersect:false}},
    plugins:{{ legend:{{labels:{{color:"#8b949e",font:{{size:11}}}}}}, tooltip:{{...TT}} }},
    scales:{{
      x:{{ticks:{{color:"#484f58",maxTicksLimit:14,maxRotation:30,font:{{size:10}}}},grid:{{color:"#21262d"}}}},
      yP:{{type:"linear",position:"left",ticks:{{color:"#8b949e",font:{{size:10}},callback:v=>(v>=0?"+":"")+v.toFixed(0)+"$"}},grid:{{color:"#21262d"}}}},
      yS2:{{type:"linear",position:"right",ticks:{{color:"#d29922",font:{{size:10}},callback:v=>"$"+Math.round(v/1000)+"k"}},grid:{{drawOnChartArea:false}}}},
    }}
  }}
}});

new Chart(document.getElementById("chartSpot"), {{
  type:"line",
  data:{{ labels:LABELS, datasets:[
    {{ label:"Spot BTC ($)", data:{spot_js}, borderColor:"#d29922", backgroundColor:"rgba(210,153,34,0.06)",
      tension:0.3, pointRadius:PT_R, borderWidth:2.5, fill:true }},
    {{ label:"CB allègement (−6% / 3j)", data:{cb_low_t1_js}, borderColor:"rgba(210,153,34,0.7)", backgroundColor:"transparent",
      tension:0.2, pointRadius:0, borderWidth:1.5, borderDash:[4,4], spanGaps:false }},
    {{ label:"CB fermeture (−10% / 3j)", data:{cb_low_js}, borderColor:"rgba(248,81,73,0.8)", backgroundColor:"transparent",
      tension:0.2, pointRadius:0, borderWidth:1.5, borderDash:[8,4], spanGaps:false }},{_strike_datasets_spot}
  ]}},
  options:{{ responsive:true, maintainAspectRatio:false,
    interaction:{{mode:"index",intersect:false}},
    plugins:{{ legend:{{labels:{{color:"#8b949e",font:{{size:11}}}}}}, tooltip:{{...TT}} }},
    scales:{{
      x:{{ticks:{{color:"#484f58",maxTicksLimit:14,maxRotation:30,font:{{size:10}}}},grid:{{color:"#21262d"}}}},
      y:{{type:"linear",position:"left",ticks:{{color:"#d29922",font:{{size:10}},callback:v=>"$"+Math.round(v/1000)+"k"}},grid:{{color:"#21262d"}}}},
    }}
  }}
}});

new Chart(document.getElementById("chartVol"), {{
  type:"line",
  data:{{ labels:LABELS, datasets:[
    {{ label:"DVOL (%)", data:{dvol_js}, borderColor:"#58a6ff", backgroundColor:"rgba(88,166,255,0.06)",
      tension:0.3, pointRadius:PT_R, borderWidth:2.5, fill:true, spanGaps:true }},
    {{ label:"HV 5j (%)", data:{hv5_js}, borderColor:"#f85149", backgroundColor:"transparent",
      tension:0.3, pointRadius:0, borderWidth:1.5, borderDash:[3,3], spanGaps:true }},
    {{ label:"HV 10j (%)", data:{hv10_js}, borderColor:"#d29922", backgroundColor:"transparent",
      tension:0.3, pointRadius:0, borderWidth:2, spanGaps:true }},
    {{ label:"HV 30j (%)", data:{hv30_js}, borderColor:"#3fb950", backgroundColor:"transparent",
      tension:0.3, pointRadius:0, borderWidth:2, borderDash:[6,3], spanGaps:true }},
    {{ label:"Seuil CB (DVOL 3j + 12pts)", data:{cb_dvol_js}, borderColor:"rgba(248,81,73,0.8)", backgroundColor:"transparent",
      tension:0.2, pointRadius:0, borderWidth:1.5, borderDash:[8,4], spanGaps:false }},
  ]}},
  options:{{ responsive:true, maintainAspectRatio:false,
    interaction:{{mode:"index",intersect:false}},
    plugins:{{ legend:{{labels:{{color:"#8b949e",font:{{size:11}}}}}}, tooltip:{{...TT}} }},
    scales:{{
      x:{{ticks:{{color:"#484f58",maxTicksLimit:14,maxRotation:30,font:{{size:10}}}},grid:{{color:"#21262d"}}}},
      y:{{type:"linear",position:"left",ticks:{{color:"#58a6ff",font:{{size:10}},callback:v=>v.toFixed(0)+"%"}},grid:{{color:"#21262d"}}}},
    }}
  }}
}});
</script>
"""

html += f"""
<footer>VRP Monitor · Généré le {generated} · GitHub Actions · Auto-refresh 5 min</footer>
</body></html>
"""

Path("docs").mkdir(exist_ok=True)
Path("docs/index.html").write_text(html, encoding="utf-8")
print(f"docs/index.html généré ({len(html)} chars)")
