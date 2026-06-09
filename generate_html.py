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
pd_map = {d.get("instrument"): d for d in positions_detail}

# scan_entry : top 5 opportunités (depuis greeks_hedge.py --run)
se_file = Path("scan_entry.json")
scan_entry = json.loads(se_file.read_text()) if se_file.exists() else {}

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
_drift         = float(s.get("hedge_delta_drift", 0))
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

# Deltas depuis dernier snapshot
_prev_spot  = float(pnl_history[-2].get("spot",      0)) if len(pnl_history) >= 2 else None
_prev_pnl   = float(pnl_history[-2].get("pnl_total", 0)) if len(pnl_history) >= 2 else None
_curr_spot  = float(s.get("spot", 0))
_curr_pnl   = float(s.get("total_pnl_usd", 0))
_delta_spot = (_curr_spot - _prev_spot)          if _prev_spot else None
_delta_spot_pct = (_delta_spot / _prev_spot * 100) if _prev_spot else None
_delta_pnl  = (_curr_pnl  - _prev_pnl)           if _prev_pnl  is not None else None

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
.alert-card { border-color: #f8514944; }
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
def _attr_card(p: dict, live: dict) -> str:
    """Carte attribution PnL pour une position."""
    instr      = p.get("instrument_name","—")
    entry_spot = float(p.get("entry_spot", spot))
    entry_p    = float(p.get("entry_price", 0))
    entry_mark = float(p.get("entry_mark_price", entry_p))
    entry_iv   = float(p.get("iv_at_entry", 0))
    gamma_e    = float(p.get("gamma_at_entry", 7e-5))

    curr_mark  = float(live.get("current_price_btc", 0))
    curr_iv    = float(live.get("current_iv_pct", entry_iv))
    d_live     = float(live.get("live_delta", 0))
    vega_live  = float(live.get("live_vega", 0))
    days_held  = float(live.get("days_held", 0))
    theta_d    = float(live.get("theta_daily_now_usd", 0))
    pnl_opt    = float(live.get("pnl_option_usd", 0))

    ds         = spot - entry_spot
    div        = curr_iv - entry_iv
    delta_pct  = abs(d_live) * 100
    gamma_pts  = gamma_e * (spot * 0.01) * 100

    pnl_delta  = abs(d_live) * ds
    pnl_gamma  = 0.5 * (-gamma_e) * ds ** 2
    pnl_theta  = theta_d * days_held
    pnl_vega   = (-vega_live) * div
    mid_mid    = (entry_mark - curr_mark) * spot
    pnl_resid  = mid_mid - (pnl_delta + pnl_gamma + pnl_theta + pnl_vega)
    ba_entry   = -(entry_mark - entry_p) * entry_spot
    total_opt  = mid_mid + ba_entry
    tte        = float(live.get("tte_days", 0))
    cl_tte     = "warn" if tte <= 1 else "neu"

    return f"""<div class="card">
  <h2>📐 Attribution PnL — {instr}</h2>
  <div style="font-size:0.75rem;color:#8b949e;margin-bottom:10px">
    ΔSpot {f(ds,0,True)}$&nbsp;·&nbsp;ΔIV {f(div,1,True)}pts&nbsp;·&nbsp;{f(days_held*24,1)}h tenu&nbsp;·&nbsp;TTE <span class="{cl_tte}">{f(tte,2)}j</span>
  </div>
  <table class="tbl">
    <tr><th style="text-align:left">Composante</th><th>Valeur ($)</th><th>% PnL opt.</th></tr>
    {_attr_row("Δ Delta",  pnl_delta, pnl_opt)}
    {_attr_row("Γ Gamma",  pnl_gamma, pnl_opt)}
    {_attr_row("Θ Theta",  pnl_theta, pnl_opt)}
    {_attr_row("ν Vega",   pnl_vega,  pnl_opt)}
    {_attr_row("~ Résidu", pnl_resid, pnl_opt)}
    <tr style="border-top:1px solid #30363d">
      <td>Mid / mid</td>
      <td class="{color(mid_mid)}">{f(mid_mid,0,True)}</td>
      <td class="muted">—</td>
    </tr>
    <tr style="border-top:1px solid #30363d;font-weight:600">
      <td>TOTAL OPTION</td>
      <td class="{color(total_opt)}">{f(total_opt,0,True)}</td>
      <td class="muted">100%</td>
    </tr>
  </table>
</div>"""

def _attr_row(label, val, total):
    pct = (val / total * 100) if abs(total) > 0.01 else 0
    return (f'<tr><td>{label}</td>'
            f'<td class="{color(val)}">{f(val,0,True)}</td>'
            f'<td class="{color(val)}" style="font-size:0.78rem">{f(pct,0,True)}%</td></tr>')

# ── Tableau des positions ──────────────────────────────────────────────────────
def _positions_table() -> str:
    if not positions_list:
        return '<p style="color:#8b949e">Aucune position ouverte.</p>'
    rows = ""
    for p in positions_list:
        instr   = p.get("instrument_name","—")
        live    = pd_map.get(instr, {})
        strike  = int(p.get("strike", 0))
        expiry  = (p.get("expiry_dt","") or "")[:10]
        tte     = float(live.get("tte_days", 0))
        cl_tte  = "warn" if tte <= 1 else ("neu" if tte > 3 else "warn")
        entry_p = float(p.get("entry_price", 0))
        entry_s = float(p.get("entry_spot", spot))
        curr_m  = float(live.get("current_price_btc", 0))
        ask     = float(live.get("current_ask_btc", 0))
        iv_e    = float(p.get("iv_at_entry", 0))
        iv_c    = float(live.get("current_iv_pct", iv_e))
        div     = iv_c - iv_e
        d_live  = float(live.get("live_delta", p.get("delta_at_entry", 0)))
        gamma   = float(live.get("live_gamma", p.get("gamma_at_entry", 0)))
        vega    = float(live.get("live_vega",  p.get("vega_at_entry",  0)))
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
        rows += f"""<tr>
      <td class="left"><b>{instr}</b></td>
      <td class="left muted" style="font-size:0.78rem">{to_ny(p.get("entry_ts","—"))}</td>
      <td>${strike:,} <span class="{cl_m}" style="font-size:0.75rem">({f(moneyness,1,True)}%)</span></td>
      <td>{expiry}</td>
      <td class="{cl_tte}"><b>{f(tte,2)}j</b></td>
      <td>{f(entry_p,5)} <span class="muted" style="font-size:0.75rem">(${f(entry_p*entry_s,0)})</span></td>
      <td>{f(curr_m,5)} <span class="muted" style="font-size:0.75rem">(ask {f(ask,5)})</span></td>
      <td>{f(iv_e,1)}% → <b>{f(iv_c,1)}%</b> <span class="{cl_iv}" style="font-size:0.75rem">({f(div,1,True)}pts)</span></td>
      <td>{f(d_live,4)}</td>
      <td style="font-size:0.75rem;color:#8b949e">{f(gamma,6)} / {f(abs(vega),2)}$</td>
      <td class="{cl_pnl}"><b>{f(pnl_opt,0,True)}$</b></td>
      <td class="{cl_pnl}">{f(pnl_pct,1,True)}%</td>
      <td style="text-align:center">{score_html}</td>
      <td style="text-align:center">{sizing_html}</td>
    </tr>"""
    return f"""<div class="card full">
  <h2>📍 Positions ouvertes — {len(positions_list)} position(s)</h2>
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
    net_gamma  = float(s.get("live_gamma", 0))
    net_vega   = float(s.get("live_vega",  0))
    net_theta  = float(s.get("theta_daily_now_usd", 0))
    gamma_pts  = abs(net_gamma) * spot * 0.01 * 100
    delta_pct  = abs(net_delta) * 100
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
    <tr><td colspan="2" style="color:#8b949e;font-size:0.75rem;padding-bottom:4px">OPTIONS (cumulé {len(positions_list)} pos.)</td></tr>
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
    pnl_opt_total = float(s.get("pnl_option_usd", 0))
    funding       = float(s.get("funding_pnl_usd", 0))
    days_held     = float(s.get("days_held", 0))
    # theta theory = somme des positions
    theta_theory  = sum(float(pd_map.get(p.get("instrument_name",""),{}).get("theta_theory_usd",0)) for p in positions_list)
    vrp           = (pnl_opt_total / theta_theory * 100) if theta_theory > 0.5 else float("nan")
    cap_cl        = "pos" if vrp >= 80 else ("warn" if vrp >= 0 else "neg")
    total_prem    = sum(float(p.get("entry_price",0))*float(p.get("entry_spot",spot)) for p in positions_list)
    pnl_pct_prem  = (pnl_open / total_prem * 100) if total_prem > 0 else 0

    return f"""<div class="card total-card">
  <h2>💰 PnL global ouvert</h2>
  <table>
    {srow("Option (MtM total)", pnl_opt_total)}
    {(lambda: row("Hedge perp (MtM + réalisé)",
        f'<span class="{color(pnl_hedge_usd)}">{f(pnl_hedge_usd,0,True)}$</span>'
        + (f'  <span style="color:#8b949e;font-size:0.75rem">'
           f'MtM: {f(pnl_hedge_usd-realized_hedge,0,True)}$'
           f'  · réalisé: {f(realized_hedge,0,True)}$</span>'
           if abs(realized_hedge) > 0.01 else "")
    ))()}
    {srow("Funding perp", funding)}
    <tr><td colspan="2"><hr style="border-color:#30363d;margin:6px 0"></td></tr>
    <tr>
      <td class="label"><b>TOTAL</b></td>
      <td class="val {color(pnl_open)} big">{f(pnl_open,0,True)}$</td>
    </tr>
    {row("% primes encaissées", f'<span class="{color(pnl_pct_prem)}">{f(pnl_pct_prem,1,True)}%</span>')}
    <tr><td colspan="2" style="color:#8b949e;font-size:0.75rem;padding-top:10px;padding-bottom:4px">THETA</td></tr>
    {row("Théorique cumulé", f'<span class="pos">+${f(theta_theory,0)}</span>  sur {f(days_held*24,1)}h')}
    {row("Capturé (PnL opt / Theta théo)",
         f'<span class="{cap_cl}">{f(vrp,0)}%</span>'
         f'<span style="color:#484f58;font-size:0.75rem"> (100% = tout capturé)</span>'
         if not math.isnan(vrp) else '<span class="neu">—</span>')}
    <tr><td colspan="2" style="color:#8b949e;font-size:0.75rem;padding-top:10px;padding-bottom:4px">STRATÉGIE CUMUL</td></tr>
    {row("Réalisé (clôtures)", f'<span class="{color(pnl_hist_total)}">{f(pnl_hist_total,0,True)}$</span>'  + (f'  <span style="color:#8b949e;font-size:0.75rem">({len(hist)} pos.)</span>' if hist else ''))}
    {row("Latent (ouvert)",    f'<span class="{color(pnl_open)}">{f(pnl_open,0,True)}$</span>')}
    <tr><td colspan="2"><hr style="border-color:#30363d;margin:6px 0"></td></tr>
    <tr>
      <td class="label"><b>TOTAL STRATÉGIE</b></td>
      <td class="val {color(pnl_cumul)}">{f(pnl_cumul,0,True)}$</td>
    </tr>
  </table>
</div>"""

# ── Tableau hedge history ──────────────────────────────────────────────────────
def _hedge_history_card() -> str:
    hh = hedge_data.get("history", [])
    if not hh:
        return ""
    rows = ""
    for h in hh:
        side    = h.get("side","?")
        side_cl = "neg" if side == "SELL" else "pos"
        rpnl    = h.get("realized_pnl_usd")
        rpnl_h  = (f'<span class="{color(rpnl)}">{f(rpnl,0,True)}$</span>'
                   if rpnl is not None else '<span style="color:#484f58">—</span>')
        rows += f"""<tr>
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
    return f"""<div class="card full">
  <h2>🔄 Historique hedge — {len(hh)} exécution(s)  <span style="font-weight:400;color:#484f58">VWAP actuel ${f(hedge_avg,2)} · Qty {f(hedge_qty,5)} BTC</span></h2>
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
</div>"""

# ── Opportunités d'entrée ─────────────────────────────────────────────────────
def _scan_entry_card() -> str:
    if not scan_entry:
        return ""
    ctx   = scan_entry.get("market_context", {})
    top5  = scan_entry.get("top5", [])
    ts_se = to_ny(scan_entry.get("ts", "—"))
    sig   = ctx.get("signal_ok", False)
    sig_cl = "pos" if sig else "neg"
    sig_lbl = "Signal OK — conditions remplies" if sig else "Signal inactif — seuils non atteints"

    rows = ""
    for i, c in enumerate(top5):
        sc   = float(c.get("score", 0))
        sc_cl = "pos" if sc >= 0.58 else ("warn" if sc >= 0.45 else "neg")
        ba   = float(c.get("ba_pct", 0))
        ba_cl = "neg" if ba > 12 else "ok"
        rows += f"""<tr {"class='hl'" if i==0 else ""}>
      <td class="left"><b>{c.get("instrument_name","—")}</b></td>
      <td class="{sc_cl}" style="font-weight:700">{f(sc,3)}</td>
      <td>${int(c.get("strike",0)):,}</td>
      <td>{f(c.get("tte_days",0),1)}j</td>
      <td>{f(c.get("delta",0),3)}</td>
      <td>{f(c.get("mark_iv",0),1)}%</td>
      <td>{f(c.get("iv_hv_ratio",0),2)}x</td>
      <td>{f(c.get("s_rank",0)*100,0)}%</td>
      <td>{f(c.get("yield_ann_pct",0),1)}%/an</td>
      <td class="{ba_cl}">{f(ba,1)}%</td>
      <td>{f(c.get("mark_price",0),5)}</td>
    </tr>"""

    return f"""<div class="card full">
  <h2>Opportunites d\'entree — scan du {ts_se}</h2>
  <div style="display:flex;gap:20px;margin-bottom:12px;font-size:0.82rem;flex-wrap:wrap">
    <span>HV10j <b>{f(ctx.get("hv_10d",0),1)}%</b></span>
    <span>IV actuelle <b>{f(ctx.get("curr_iv",0),1)}%</b></span>
    <span>IV/HV <b>{f(ctx.get("iv_hv_ratio",0),2)}x</b></span>
    <span>Regime <b>{ctx.get("regime","—")}</b></span>
    <span class="{sig_cl}"><b>{sig_lbl}</b></span>
  </div>
  <div style="overflow-x:auto">
  <table class="tbl">
    <tr>
      <th style="text-align:left">Instrument</th>
      <th>Score</th><th>Strike</th><th>TTE</th><th>Delta</th>
      <th>IV</th><th>IV/HV</th><th>Rang IV</th><th>Yield ann.</th><th>B/A</th><th>Mark</th>
    </tr>
    {rows if rows else '<tr><td colspan="11" class="muted" style="text-align:center">Aucun candidat</td></tr>'}
  </table>
  </div>
  <div style="margin-top:14px;font-size:0.78rem;color:#8b949e;border-top:1px solid #21262d;padding-top:10px">
    <b style="color:#e6edf3">Méthodologie scoring</b> :
    Score = 40% IV/HV + 30% rang IV + 30% yield annualisé ·
    <b>Seuils d\'entrée</b> : score ≥ 0.58 · IV/HV ≥ 1.10 · B/A ≤ 12% ·
    <b>Sizing</b> : round(score, 1) BTC · max 3 BTC portefeuille
  </div>
</div>"""

# ── Alertes ────────────────────────────────────────────────────────────────────
def _alerts_card() -> str:
    tte_min  = min((float(pd_map.get(p.get("instrument_name",""),{}).get("tte_days", 99)) for p in positions_list), default=99)
    div_main = float(s.get("iv_change", 0))
    total_prem = sum(float(p.get("entry_price",0))*float(p.get("entry_spot",spot)) for p in positions_list)
    alerts = []
    if tte_min <= 1:  alerts.append(("neg", "⚠️ ROLLER",        f"TTE min = {f(tte_min,2)}j"))
    else:             alerts.append(("ok",  "✅ Roll OK",        f"TTE min = {f(tte_min,2)}j"))
    if _drift_abs > hedge_thr_btc:
                      alerts.append(("neg", "⚠️ REBALANCER",    f"Drift = {f(_drift_pct,2)}% > seuil {f(hedge_thr_pct,1)}%"))
    else:             alerts.append(("ok",  "✅ Hedge OK",       f"Drift = {f(_drift_pct,2)}% < seuil {f(hedge_thr_pct,1)}%"))
    if div_main > 10: alerts.append(("neg", "🚨 IV SPIKE",       f"ΔIV = {f(div_main,1,True)}pts"))
    elif div_main > 5:alerts.append(("warn","⚠️ IV élevée",      f"ΔIV = {f(div_main,1,True)}pts"))
    else:             alerts.append(("ok",  "✅ IV OK",          f"ΔIV = {f(div_main,1,True)}pts"))
    if pnl_open < -total_prem:
                      alerts.append(("neg", "🚨 STOP-LOSS",      f"Perte {f(pnl_open,0,True)}$"))
    else:             alerts.append(("ok",  "✅ Stop OK",        f"Perte latente {f(pnl_open,0,True)}$ / prime {f(total_prem,0)}$"))
    rows = "".join(f'<tr><td class="val {cl}" style="width:28%">{lbl}</td><td style="color:#8b949e">{det}</td></tr>'
                   for cl, lbl, det in alerts)
    return f'<div class="card alert-card full"><h2>🚨 Alertes</h2><table>{rows}</table></div>'

# ── HTML ───────────────────────────────────────────────────────────────────────
title    = f"VRP Monitor — {len(positions_list)} position(s)" if positions_list else "VRP Monitor"
_spot_cl = "pos" if (_delta_spot or 0) >= 0 else "neg"
_pnl_cl  = "pos" if _curr_pnl >= 0 else "neg"
_dpnl_cl = ("pos" if (_delta_pnl or 0) >= 0 else "neg") if _delta_pnl is not None else "neu"
_tte_min = min((float(pd_map.get(p.get("instrument_name",""),{}).get("tte_days",99)) for p in positions_list), default=99)
_tte_cl  = "warn" if _tte_min <= 1 else "neu"

_spot_delta_html = (
    f'<span class="chip-delta {_spot_cl}">{f(_delta_spot,0,True)}$ ({f(_delta_spot_pct,2,True)}%) vs snapshot préc.</span>'
) if _delta_spot is not None else '<span class="chip-delta neu">— premier snapshot</span>'

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
<h1>📊 VRP Monitor — {len(positions_list)} position(s) ouverte(s)</h1>
<div class="subtitle">Données : {ts} · Généré : {generated} · ↻ auto-refresh 5min</div>

<div class="header-bar">
  <div class="chip">
    <span class="chip-label">Spot BTC</span>
    <span class="chip-value">${f(_curr_spot,0)}</span>
    {_spot_delta_html}
  </div>
  <div class="chip">
    <span class="chip-label">PnL total</span>
    <span class="chip-value {_pnl_cl}">{f(_curr_pnl,0,True)}$</span>
    {_pnl_delta_html}
  </div>
  <div class="chip">
    <span class="chip-label">Positions</span>
    <span class="chip-value">{len(positions_list)}</span>
    <span class="chip-delta neu">TTE min <span class="{_tte_cl}">{f(_tte_min,2)}j</span></span>
  </div>
  <div class="chip">
    <span class="chip-label">Hedge drift</span>
    <span class="chip-value {_drift_cl}">{f(_drift_pct,2)}%</span>
    <span class="chip-delta neu">Seuil {f(hedge_thr_pct,1)}% · {"⚠️ REBALANCER" if _drift_abs>hedge_thr_btc else "✅ OK"}</span>
  </div>
  <div class="chip">
    <span class="chip-label">Net delta (pos+hedge)</span>
    <span class="chip-value {_drift_cl}">{f(_drift*100,2,True)}%</span>
    <span class="chip-delta neu">{f(_drift,4,True)} BTC ≈ {f(_drift*spot,0,True)}$</span>
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

    for p in positions_list:
        instr = p.get("instrument_name","")
        live  = pd_map.get(instr, {})
        if live:
            html += _attr_card(p, live)
        else:
            html += f'<div class="card"><h2>📐 {instr}</h2><p style="color:#8b949e;font-size:0.85rem">Données live non disponibles (prochain cycle Actions)</p></div>'

    html += "</div>\n<div class=\"grid\" style=\"margin-top:16px\">\n"
    html += _scan_entry_card()
    html += _hedge_history_card()
    html += _alerts_card()

    # Historique des clôtures
    if hist:
        h_rows = ""
        for h in hist:
            pnl_h = float(h.get("pnl_usd",0))
            h_rows += f"""<tr>
          <td class="left">{h.get("instrument_name","?")}</td>
          <td class="muted">{str(h.get("entry_ts",""))[:10]}</td>
          <td class="muted">{str(h.get("exit_ts",""))[:10]}</td>
          <td class="{color(pnl_h)}"><b>{f(pnl_h,0,True)}$</b></td>
          <td class="muted">{h.get("exit_reason","—")}</td>
        </tr>"""
        html += f"""<div class="card full">
  <h2>📈 Positions clôturées — {len(hist)} roll(s)</h2>
  <table class="tbl">
    <tr><th style="text-align:left">Instrument</th><th style="text-align:left">Entrée</th>
        <th style="text-align:left">Sortie</th><th>PnL</th><th style="text-align:left">Raison</th></tr>
    {h_rows}
    <tr style="border-top:1px solid #30363d;font-weight:600">
      <td colspan="3">TOTAL RÉALISÉ</td>
      <td class="{color(pnl_hist_total)}">{f(pnl_hist_total,0,True)}$</td>
      <td></td>
    </tr>
  </table>
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
    pnl_tot      = [p.get("pnl_total",0)    for p in pnl_history]
    spot_data    = [p.get("spot",0)         for p in pnl_history]
    n_pos_data   = [p.get("n_positions",1)  for p in pnl_history]

    labels_js    = _json.dumps(labels)
    delta_js     = _json.dumps(delta_data)
    net_delta_js = _json.dumps(net_d_data)
    gamma_js     = _json.dumps(gamma_data)
    pnl_opt_js   = _json.dumps(pnl_opt)
    pnl_hdg_js   = _json.dumps(pnl_hdg)
    pnl_tot_js   = _json.dumps(pnl_tot)
    spot_js      = _json.dumps(spot_data)
    n_pts        = len(pnl_history)

    html += f"""
<div class="chart-section">

<div class="chart-card">
  <h2>📈 Greeks &amp; Spot — {n_pts} snapshots</h2>
  <div class="chart-wrap"><canvas id="chartGreeks"></canvas></div>
</div>

<div class="chart-card">
  <h2>💰 PnL &amp; Spot — dans le temps</h2>
  <div class="chart-wrap"><canvas id="chartPnl"></canvas></div>
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
      yAxisID:"yS", tension:0.3, pointRadius:0, borderWidth:1.5, borderDash:[2,4] }},
  ]}},
  options:{{ responsive:true, maintainAspectRatio:false,
    interaction:{{mode:"index",intersect:false}},
    plugins:{{ legend:{{labels:{{color:"#8b949e",font:{{size:11}}}}}}, tooltip:{{...TT}} }},
    scales:{{
      x:{{ticks:{{color:"#484f58",maxTicksLimit:14,maxRotation:30,font:{{size:10}}}},grid:{{color:"#21262d"}}}},
      yD:{{type:"linear",position:"left",ticks:{{color:"#58a6ff",font:{{size:10}},callback:v=>v.toFixed(1)+"%"}},grid:{{color:"#21262d"}}}},
      yG:{{type:"linear",position:"right",ticks:{{color:"#f85149",font:{{size:10}},callback:v=>v.toFixed(2)}},grid:{{drawOnChartArea:false}}}},
      yS:{{type:"linear",position:"right",ticks:{{color:"#d29922",font:{{size:10}},callback:v=>"$"+Math.round(v/1000)+"k"}},grid:{{drawOnChartArea:false}},offset:true}},
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
      yAxisID:"yS2", tension:0.3, pointRadius:0, borderWidth:1.5, borderDash:[2,4] }},
  ]}},
  options:{{ responsive:true, maintainAspectRatio:false,
    interaction:{{mode:"index",intersect:false}},
    plugins:{{ legend:{{labels:{{color:"#8b949e",font:{{size:11}}}}}}, tooltip:{{...TT}} }},
    scales:{{
      x:{{ticks:{{color:"#484f58",maxTicksLimit:14,maxRotation:30,font:{{size:10}}}},grid:{{color:"#21262d"}}}},
      yP:{{type:"linear",position:"left",ticks:{{color:"#8b949e",font:{{size:10}},callback:v=>(v>=0?"+":"")+v.toFixed(0)+"$"}},grid:{{color:"#21262d"}}}},
      yS2:{{type:"linear",position:"right",ticks:{{color:"#d29922",font:{{size:10}},callback:v=>"$"+Math.round(v/1000)+"k"}},grid:{{drawOnChartArea:false}},offset:true}},
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
