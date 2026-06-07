"""
generate_html.py — Génère docs/index.html depuis pnl_summary.json + positions.json
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
    _TZ_NY = None  # fallback manuel ci-dessous

def to_ny(ts_str) -> str:
    """Convertit un timestamp UTC en heure NY (EDT/EST).
    Accepte : datetime object, 'YYYY-MM-DD HH:MM:SS UTC',
              'YYYY-MM-DD HH:MM UTC', 'YYYY-MM-DDTHH:MM:SS+00:00'.
    Retourne : 'YYYY-MM-DD HH:MM EDT' (ou EST en hiver).
    """
    if not ts_str or ts_str == "—":
        return str(ts_str)
    # --- parsing ---
    if isinstance(ts_str, datetime):
        dt = ts_str if ts_str.tzinfo else ts_str.replace(tzinfo=timezone.utc)
    else:
        s = str(ts_str).strip()
        dt = None
        # Formats à essayer dans l'ordre (sans manipuler la chaîne globalement)
        candidates = [
            ("%Y-%m-%d %H:%M:%S UTC", s),
            ("%Y-%m-%d %H:%M UTC",    s),
            ("%Y-%m-%dT%H:%M:%S+00:00", s),
            ("%Y-%m-%d %H:%M:%S",     s.replace(" UTC", "")),
            ("%Y-%m-%d %H:%M",        s.replace(" UTC", "")),
        ]
        for fmt, val in candidates:
            try:
                dt = datetime.strptime(val, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        if dt is None:
            return s  # impossible à parser → retour brut
    # --- conversion NY ---
    if _TZ_NY:
        dt_ny = dt.astimezone(_TZ_NY)
        label = dt_ny.strftime("%Z")   # "EDT" ou "EST"
    else:
        # Calcul manuel DST : 2ème dim mars 02h → 1er dim nov 02h
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

pos   = pos_raw.get("open")
hist  = pos_raw.get("history", [])

# Historique pour graphiques
history_file = Path("pnl_history.json")
pnl_history  = json.loads(history_file.read_text()) if history_file.exists() else []

no_position = pos is None and not hist

# ── Helpers ────────────────────────────────────────────────────────────────────
def f(v, decimals=2, sign=False):
    """Formate un nombre float depuis string ou float."""
    try:
        n = float(v)
        fmt = f"{{:+,.{decimals}f}}" if sign else f"{{:,.{decimals}f}}"
        return fmt.format(n)
    except (TypeError, ValueError):
        return str(v) if v else "—"

def color(v, invert=False):
    """Classe CSS selon signe."""
    try:
        n = float(v)
        if invert:
            n = -n
        return "pos" if n > 0 else ("neg" if n < 0 else "neu")
    except:
        return "neu"

# ── Calculs attribution ────────────────────────────────────────────────────────
attr = {}
matrix_rows = []

if pos and summ_raw.get("spot"):
    s = summ_raw
    spot         = float(s.get("spot", 0))
    entry_spot   = float(pos.get("entry_spot", spot))
    entry_price  = float(pos.get("entry_price", 0))
    entry_mark   = float(pos.get("entry_mark_price", 0))
    entry_iv     = float(pos.get("iv_at_entry", 0))
    # Gamma : préférer live_gamma (pnl_summary.json) → recalculé à chaque run Actions
    # Fallback sur gamma_at_entry uniquement si le champ live n'est pas encore présent
    gamma_entry  = float(s.get("live_gamma") or pos.get("gamma_at_entry", 7e-5))
    hedge_avg    = float(pos.get("hedge_avg_entry", entry_spot))
    hedge_qty        = float(s.get("hedge_qty", pos.get("hedge_qty", 0)))
    hedge_thr_pct    = float(s.get("hedge_threshold_pct") or 5.0)
    hedge_thr_btc    = float(s.get("hedge_threshold_btc") or hedge_thr_pct / 100)
    curr_mark    = float(s.get("current_price_btc", 0))
    curr_ask     = float(s.get("current_ask_btc", 0))
    curr_bid     = float(s.get("current_bid_btc", 0))
    curr_iv      = float(s.get("current_iv_pct", entry_iv))
    delta_live   = float(s.get("live_delta", 0))
    vega_live    = float(s.get("live_vega", 0))
    theta_daily  = float(s.get("theta_daily_now_usd", 0))
    days_held    = float(s.get("days_held", 0))
    tte_days     = float(s.get("tte_days", 0))

    delta_spot   = spot - entry_spot
    delta_iv     = curr_iv - entry_iv
    hours_held   = days_held * 24
    delta_pct    = abs(delta_live) * 100
    gamma_pts    = gamma_entry * (spot * 0.01) * 100

    pnl_delta    = abs(delta_live) * delta_spot
    pnl_gamma    = 0.5 * (-gamma_entry) * delta_spot ** 2
    pnl_theta    = theta_daily * days_held
    pnl_vega     = (-vega_live) * delta_iv
    mid_to_mid   = (entry_mark - curr_mark) * spot
    pnl_residual = mid_to_mid - (pnl_delta + pnl_gamma + pnl_theta + pnl_vega)
    ba_entry     = -(entry_mark - entry_price) * entry_spot
    ba_exit      = -(curr_ask - curr_mark) * spot
    total_option = mid_to_mid + ba_entry + ba_exit

    # ── Barre de drift hedge ───────────────────────────────────────────────
    _drift_abs    = abs(float(s.get("hedge_delta_drift", 0)))
    _drift_pct    = _drift_abs * 100
    _fill_pct     = min(100.0, _drift_abs / max(hedge_thr_btc, 1e-9) * 100)
    _bar_color    = "#f85149" if _drift_abs > hedge_thr_btc else ("#d29922" if _fill_pct > 70 else "#3fb950")
    _drift_cl     = "warn" if _drift_abs > hedge_thr_btc else ("warn" if _fill_pct > 70 else "ok")
    _warn_label   = f(hedge_thr_pct * 0.7, 1)
    drift_bar_html = f"""
  <div style="margin-top:12px">
    <div style="display:flex;justify-content:space-between;font-size:0.75rem;color:#8b949e;margin-bottom:4px">
      <span>Drift actuel&nbsp;: <b class="{_drift_cl}">{f(_drift_pct,2)}%&thinsp;&#916;</b></span>
      <span>Seuil&nbsp;: {f(hedge_thr_pct,1)}%&thinsp;&#916;</span>
    </div>
    <div style="background:#21262d;border-radius:4px;height:8px;position:relative;overflow:hidden">
      <div style="height:8px;border-radius:4px;width:{_fill_pct:.1f}%;background:{_bar_color}"></div>
      <div style="position:absolute;top:0;right:0;bottom:0;width:2px;background:#484f58"></div>
    </div>
    <div style="display:flex;justify-content:space-between;font-size:0.7rem;color:#484f58;margin-top:3px">
      <span>0%</span><span style="color:#8b949e">{_warn_label}% &#9888;</span><span>{f(hedge_thr_pct,1)}% &#128308;</span>
    </div>
  </div>"""

    attr = dict(
        spot=spot, entry_spot=entry_spot, delta_spot=delta_spot,
        delta_iv=delta_iv, hours_held=hours_held,
        delta_pct=delta_pct, gamma_pts=gamma_pts,
        pnl_delta=pnl_delta, pnl_gamma=pnl_gamma,
        pnl_theta=pnl_theta, pnl_vega=pnl_vega,
        mid_to_mid=mid_to_mid, pnl_residual=pnl_residual,
        ba_entry=ba_entry, ba_exit=ba_exit, total_option=total_option,
        entry_price=entry_price, entry_mark=entry_mark,
        curr_ask=curr_ask, curr_mark=curr_mark,
        theta_daily=theta_daily, tte_days=tte_days,
        delta_live=delta_live, vega_live=vega_live,
    )

    # Matrice ±5%
    premium_usd = entry_price * entry_spot
    for pct in [-5, -3, -2, -1, 0, 1, 2, 3, 5]:
        ds        = spot * pct / 100
        ns        = spot + ds
        # Put : spot baisse → plus ITM → delta monte ; spot monte → plus OTM → delta baisse
        nd_pct    = (delta_pct + gamma_pts * abs(pct)) if pct < 0 else max(0.0, delta_pct - gamma_pts * abs(pct))
        pnl_o     = abs(delta_live) * ds - 0.5 * gamma_entry * ds ** 2
        if pct > 0:
            pnl_o = min(pnl_o, premium_usd)
        pnl_h     = -abs(hedge_qty) * (ns - hedge_avg)
        pnl_n     = pnl_o + pnl_h
        matrix_rows.append((pct, ns, nd_pct, pnl_o, pnl_h, pnl_n))

# ── PnL historique ─────────────────────────────────────────────────────────────
pnl_hist_total = sum(float(h.get("pnl_usd", 0)) for h in hist)
pnl_open       = float(summ_raw.get("total_pnl_usd", 0)) if pos else 0.0
pnl_cumul      = pnl_hist_total + pnl_open

# ── Timestamp ─────────────────────────────────────────────────────────────────
ts = to_ny(summ_raw.get("timestamp", "—"))
generated = to_ny(datetime.now(timezone.utc))

# ── Deltas depuis le dernier snapshot (pour l'en-tête) ────────────────────────
_prev_spot    = float(pnl_history[-2].get("spot",      0)) if len(pnl_history) >= 2 else None
_prev_pnl     = float(pnl_history[-2].get("pnl_total", 0)) if len(pnl_history) >= 2 else None
_curr_spot    = float(summ_raw.get("spot", 0))
_curr_pnl     = float(summ_raw.get("total_pnl_usd", 0))
_delta_spot   = (_curr_spot - _prev_spot)           if _prev_spot is not None else None
_delta_spot_pct = (_delta_spot / _prev_spot * 100)  if _prev_spot else None
_delta_pnl    = (_curr_pnl  - _prev_pnl)            if _prev_pnl  is not None else None

# ── HTML ───────────────────────────────────────────────────────────────────────
def row(label, value, cls="", unit=""):
    return f'<tr><td class="label">{label}</td><td class="{cls}">{value}{unit}</td></tr>'

def srow(label, val, invert=False, unit="$", decimals=0):
    v = f(val, decimals, sign=True)
    cl = color(val, invert)
    u = f" {unit}" if unit else ""
    return f'<tr><td class="label">{label}</td><td class="val {cl}">{v}{u}</td></tr>'

html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<title>VRP Monitor — {pos["instrument_name"] if pos else "Aucune position"}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'SF Mono', 'Fira Code', monospace; background: #0d1117; color: #e6edf3; min-height: 100vh; padding: 24px; }}
  h1 {{ font-size: 1.4rem; color: #58a6ff; margin-bottom: 10px; }}
  .subtitle {{ color: #8b949e; font-size: 0.82rem; margin-bottom: 6px; }}
  /* En-tête chips */
  .header-bar {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 20px; align-items: center; }}
  .chip {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 8px 14px; font-size: 0.82rem; display: flex; flex-direction: column; gap: 2px; min-width: 140px; }}
  .chip-label {{ color: #8b949e; font-size: 0.70rem; text-transform: uppercase; letter-spacing: .06em; }}
  .chip-value {{ font-weight: 700; font-size: 1.05rem; color: #e6edf3; }}
  .chip-delta {{ font-size: 0.75rem; margin-top: 1px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 16px; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 20px; }}
  .card h2 {{ font-size: 0.78rem; text-transform: uppercase; letter-spacing: .1em; color: #8b949e; border-bottom: 1px solid #21262d; padding-bottom: 10px; margin-bottom: 14px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.88rem; }}
  td {{ padding: 5px 4px; vertical-align: top; }}
  td.label {{ color: #8b949e; width: 52%; }}
  td.val {{ text-align: right; font-weight: 600; }}
  .pos {{ color: #3fb950; }}
  .neg {{ color: #f85149; }}
  .neu {{ color: #e6edf3; }}
  .warn {{ color: #d29922; }}
  .ok  {{ color: #3fb950; }}
  .big {{ font-size: 1.6rem; font-weight: 700; }}
  .total-card {{ border-color: #388bfd44; }}
  .alert-card {{ border-color: #f8514944; }}

  /* Matrice */
  .matrix {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
  .matrix th {{ color: #8b949e; font-weight: 500; padding: 6px 8px; text-align: right; border-bottom: 1px solid #21262d; }}
  .matrix th:first-child {{ text-align: left; }}
  .matrix td {{ padding: 5px 8px; text-align: right; border-bottom: 1px solid #1c2128; }}
  .matrix td:first-child {{ text-align: left; color: #8b949e; }}
  .matrix tr.zero {{ background: #1c2128; }}
  .matrix tr:hover {{ background: #21262d; }}

  /* Historique */
  .hist-row {{ display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #21262d; font-size: 0.85rem; }}
  .hist-row:last-child {{ border-bottom: none; }}

  /* Barre theta */
  .progress-bg {{ background: #21262d; border-radius: 4px; height: 6px; margin-top: 6px; }}
  .progress-fill {{ background: #3fb950; border-radius: 4px; height: 6px; }}

  /* Graphiques pleine largeur */
  .chart-section {{ margin-top: 16px; display: flex; flex-direction: column; gap: 16px; }}
  .chart-card {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 20px; }}
  .chart-card h2 {{ font-size: 0.78rem; text-transform: uppercase; letter-spacing: .1em; color: #8b949e; border-bottom: 1px solid #21262d; padding-bottom: 10px; margin-bottom: 14px; }}
  .chart-wrap {{ position: relative; height: 360px; }}

  footer {{ text-align: center; color: #484f58; font-size: 0.75rem; margin-top: 28px; }}
</style>
</head>
<body>
"""

if no_position:
    html += "<h1>Aucune position ouverte.</h1></body></html>"
else:
    s = summ_raw
    instrument = pos["instrument_name"] if pos else "—"
    expiry     = pos.get("expiry_dt", "—")[:10] if pos else "—"
    spot_move  = f(float(s.get("spot_move_pct", 0)), 2, sign=True)

    # ── Chips de l'en-tête ────────────────────────────────────────────────────
    _spot_cl  = "pos" if (_delta_spot or 0) >= 0 else "neg"
    _pnl_cl   = "pos" if _curr_pnl >= 0 else "neg"
    _dpnl_cl  = ("pos" if (_delta_pnl or 0) >= 0 else "neg") if _delta_pnl is not None else "neu"
    _tte_cl   = "warn" if float(s.get("tte_days", 99)) <= 1 else "neu"

    _spot_delta_html = (
        f'<span class="chip-delta {_spot_cl}">'
        f'{f(_delta_spot, 0, True)}$'
        f' ({f(_delta_spot_pct, 2, True)}%) vs snapshot préc.</span>'
    ) if _delta_spot is not None else '<span class="chip-delta neu">— premier snapshot</span>'

    _pnl_delta_html = (
        f'<span class="chip-delta {_dpnl_cl}">'
        f'{f(_delta_pnl, 0, True)}$ vs snapshot préc.</span>'
    ) if _delta_pnl is not None else '<span class="chip-delta neu">— premier snapshot</span>'

    html += f"""
<h1>📊 {instrument}</h1>
<div class="subtitle">Données : {ts} · Généré : {generated} · ↻ auto-refresh 5min</div>

<div class="header-bar">
  <div class="chip">
    <span class="chip-label">Spot BTC</span>
    <span class="chip-value">${f(_curr_spot, 0)}</span>
    {_spot_delta_html}
  </div>
  <div class="chip">
    <span class="chip-label">PnL total</span>
    <span class="chip-value {_pnl_cl}">{f(_curr_pnl, 0, True)}$</span>
    {_pnl_delta_html}
  </div>
  <div class="chip">
    <span class="chip-label">TTE restant</span>
    <span class="chip-value {_tte_cl}">{f(s.get("tte_days"), 2)}j</span>
    <span class="chip-delta neu">Expiry {expiry}</span>
  </div>
  <div class="chip">
    <span class="chip-label">IV actuelle</span>
    <span class="chip-value {"neg" if float(s.get("current_iv_pct",0)) > float(pos.get("iv_at_entry",0)) else "pos"}">{f(s.get("current_iv_pct"), 1)}%</span>
    <span class="chip-delta {"neg" if float(s.get("current_iv_pct",0)) > float(pos.get("iv_at_entry",0)) else "pos"}">{f(float(s.get("current_iv_pct",0)) - float(pos.get("iv_at_entry",0)), 1, True)} pts vs entrée</span>
  </div>
</div>

<div class="grid">

<!-- POSITION -->
<div class="card">
  <h2>📍 Position & Nominal</h2>
  <table>
    {row("Instrument", f'<b>{instrument}</b>')}
    {row("Strike / Expiry", f'${int(pos["strike"]):,}  ·  {expiry}')}
    {row("TTE restant", f'<b class="{"warn" if float(s.get("tte_days",2))<=1 else "neu"}">{f(s.get("tte_days"),2)} jours</b>')}
    {row("Spot actuel", f'<b>${f(s.get("spot"),0)}</b>  <span class="{"pos" if float(s.get("spot_move_pct",0))>=0 else "neg"}">{spot_move}%</span>')}
    {row("IV actuelle", f'{f(s.get("current_iv_pct"),1)}%  <span class="{"neg" if float(s.get("current_iv_pct",0))>float(pos.get("iv_at_entry",0)) else "pos"}">{f(attr.get("delta_iv",0),1,True)}pts</span>')}
    {row("Nominal brut", f'${int(pos["strike"] * int(pos.get("contracts",1))):,}')}
    {row("Expo delta-ajustée", f'${f(attr.get("delta_pct",0)/100 * attr.get("spot",0), 0)}')}
    {row("Prime encaissée", f'{pos.get("entry_price")} BTC = <b>${f(float(pos.get("entry_price",0)) * float(pos.get("entry_spot",0)), 0)}</b>')}
    {row("Rachat (ask)", f'{s.get("current_ask_btc")} BTC = ${f(float(s.get("current_ask_btc",0))*float(s.get("spot",0)),0)}')}
    <tr><td colspan="2" style="padding-top:10px; color:#8b949e; font-size:0.78rem;">ENTRÉE</td></tr>
    {row("Date d'entrée", to_ny(pos.get("entry_ts","—")))}
    {row("Spot à l'entrée", f'${f(pos.get("entry_spot"),0)}')}
    {row("Prix vendu (bid)", f'{pos.get("entry_price")} BTC  ({f(float(pos.get("entry_price",0))*100/float(pos.get("entry_mark_price",1)),1)}% vs mark)')}
    {row("Mark à l'entrée", f'{pos.get("entry_mark_price")} BTC')}
    {row("IV à l'entrée", f'{pos.get("iv_at_entry")}%')}
    {row("Delta à l'entrée", f'{pos.get("delta_at_entry")}')}
  </table>
</div>

<!-- GREEKS -->
<div class="card">
  <h2>📐 Greeks (short, signés)</h2>
  <table>
    {row("Δ Delta", f'<b>+{f(attr.get("delta_pct",0),1)}%</b>  ({f(abs(attr.get("delta_live",0)),4)} BTC)')}
    {row("Γ Gamma", f'<span class="neg">−{f(attr.get("gamma_pts",0),2)} pts Δ / 1% move</span>')}
    {row("ν Vega", f'<span class="neg">−{f(attr.get("vega_live",0),2)}</span>  (−${f(attr.get("vega_live",0),0)} / +1pt IV)')}
    {row("Θ Theta", f'<span class="pos">+${f(attr.get("theta_daily",0),0)}/jour</span>')}
    {row("Theta restant (théo)", f'<span class="pos">+${f(attr.get("theta_daily",0) * attr.get("tte_days",0),0)}</span>  sur {f(attr.get("tte_days",0),2)}j')}
    <tr><td colspan="2" style="padding-top:10px; color:#8b949e; font-size:0.78rem;">HEDGE PERP</td></tr>
    {row("Qty short", f'{s.get("hedge_qty")} BTC')}
    {row("VWAP entrée", f'${f(pos.get("hedge_avg_entry"),0)}')}
    {row("Rebalancements", str(pos.get("hedge_rebalances", 0)))}
    {(lambda drift=float(s.get("hedge_delta_drift",0)), net_usd=float(s.get("hedge_delta_drift",0))*float(s.get("spot",0)):
      row("Delta net (pos+hedge)",
          f'<b class="{"warn" if abs(drift)>hedge_thr_btc else ("pos" if drift>0 else "neg")}">'
          f'{f(drift*100,2,True)}%</b>'
          f'  ({f(drift,4,True)} BTC'
          f'  ≈ {f(net_usd,0,True)}$)'
      ))()}
    {(lambda drift=float(s.get("hedge_delta_drift",0)):
      row("Seuil rebal. (IV-adj)",
          f'<span class="neu">{f(hedge_thr_pct,1)}% delta = {f(hedge_thr_btc,4)} BTC</span>'
          f'  <span class="{"warn" if abs(drift)>hedge_thr_btc else "ok"}" style="font-size:0.8rem">'
          f'{"⚠️ REBALANCER" if abs(drift)>hedge_thr_btc else "✅ OK"}</span>'
      ))()}
    {(lambda hh=pos.get("hedge_history",[]), ts_snap=summ_raw.get("timestamp",""):
      row("Dernier rebal. ce cycle",
          (lambda last=hh[-1] if hh else None:
            (f'<span class="ok">✅ Exécuté {to_ny(last["ts"])} — '
             f'{"BUY" if last.get("qty",0)>0 else "SELL"} {f(abs(last.get("qty",0)),5)} BTC @ ${f(last.get("spot",0),0)}'
             f'<br><span style="font-size:0.78rem;color:#8b949e">'
             f'Qty: {f(last.get("qty_before",0),5)} → {f(last.get("qty_after",0),5)} BTC'
             f'  · VWAP: ${f(last.get("vwap_before",0),2)} → ${f(last.get("vwap_after",0),2)}'
             + (f'  · {last["note"]}' if last.get("note") else "")
             + f'</span></span>'
             if last else '<span class="neu">—</span>')
          )()
      ) if hh else row("Dernier rebal. ce cycle", '<span class="neu">Aucun rebalancement</span>')
    )()}
  </table>
  {drift_bar_html}
</div>

<!-- PnL OPEN -->
<div class="card total-card">
  <h2>💰 PnL Position Ouverte</h2>
  <table>
    {srow("Option MtM", s.get("pnl_option_usd"), invert=False)}
    {srow("Hedge perp", s.get("pnl_hedge_usd"), invert=False)}
    <tr><td colspan="2"><hr style="border-color:#30363d;margin:6px 0"></td></tr>
    <tr><td class="label"><b>TOTAL</b></td>
        <td class="val {color(s.get('total_pnl_usd'))} big">{f(s.get("total_pnl_usd"),0,True)}$</td></tr>
    {row("% de la prime", f'<span class="{color(s.get("pnl_pct_of_premium"))}">{f(s.get("pnl_pct_of_premium"),1,True)}%</span>')}
    <tr><td colspan="2" style="padding-top:10px; color:#8b949e; font-size:0.78rem;">THETA</td></tr>
    {row("Cumulé théorique", f'<span class="pos">+${f(s.get("theta_theory_usd"),0)}</span>  sur {f(attr.get("hours_held",0),1)}h')}
    {(lambda cap=float(s.get("vrp_capture_pct") or 0), th=float(s.get("theta_theory_usd") or 0):
      row("Theta capturé",
          f'<span class="{"pos" if cap >= 80 else ("warn" if cap >= 0 else "neg")}">'
          f'{f(cap, 0)}%</span>'
          f'<span style="color:#484f58;font-size:0.78rem"> = PnL option / Theta théo.</span>'
          f'<br><span style="color:#484f58;font-size:0.72rem">'
          f'100% = tout le theta capturé · &gt;100% = IV compression bonus · &lt;0% = IV/Δ mangent tout</span>'
      ))()}
  </table>
  <div class="progress-bg" title="Theta cumulé / Prime">
    <div class="progress-fill" style="width:{min(100, abs(float(s.get('theta_theory_usd',0))/float(pos.get('entry_price_usd',400))*100)):.0f}%"></div>
  </div>
  <div style="font-size:0.72rem;color:#484f58;margin-top:4px">Theta accumulé vs prime encaissée</div>
</div>

<!-- ATTRIBUTION -->
<div class="card">
  <h2>📐 Attribution PnL  <span style="font-weight:400;color:#484f58">ΔSpot {f(attr.get("delta_spot",0),0,True)}$  ·  ΔIV {f(attr.get("delta_iv",0),1,True)}pts  ·  {f(attr.get("hours_held",0),1)}h</span></h2>
  <table>
    {srow("Δ Delta", attr.get("pnl_delta",0))}
    {srow("Γ Gamma", attr.get("pnl_gamma",0))}
    {srow("Θ Theta", attr.get("pnl_theta",0))}
    {srow("ν Vega", attr.get("pnl_vega",0))}
    {srow("~ Résidu", attr.get("pnl_residual",0))}
    <tr><td colspan="2"><hr style="border-color:#30363d;margin:6px 0"></td></tr>
    {srow("Mid / mid", attr.get("mid_to_mid",0))}
    {srow("BA entrée", attr.get("ba_entry",0))}
    {srow("BA sortie", attr.get("ba_exit",0))}
    <tr><td colspan="2"><hr style="border-color:#30363d;margin:6px 0"></td></tr>
    {srow("TOTAL OPTION", attr.get("total_option",0))}
  </table>
</div>

<!-- HISTORIQUE -->
<div class="card">
  <h2>📈 PnL Cumulé — Toutes Positions</h2>
"""
    if not hist:
        html += '<p style="color:#8b949e;font-size:0.85rem;">(Première position — pas encore de clôture)</p>'
    else:
        for h in hist:
            pnl_h = float(h.get("pnl_usd", 0))
            cl    = "pos" if pnl_h >= 0 else "neg"
            html += f"""
  <div class="hist-row">
    <span style="color:#8b949e">{h.get("instrument_name","?")}  <span style="font-size:0.75rem">{str(h.get("entry_ts",""))[:10]}</span></span>
    <span class="{cl}">{f(pnl_h,0,True)} $</span>
  </div>"""

    cl_hist  = color(pnl_hist_total)
    cl_open  = color(pnl_open)
    cl_cumul = color(pnl_cumul)
    html += f"""
  <table style="margin-top:14px">
    <tr><td class="label">Réalisé ({len(hist)} position(s))</td><td class="val {cl_hist}">{f(pnl_hist_total,0,True)} $</td></tr>
    <tr><td class="label">Latent (ouverte)</td><td class="val {cl_open}">{f(pnl_open,0,True)} $</td></tr>
    <tr><td colspan="2"><hr style="border-color:#30363d;margin:6px 0"></td></tr>
    <tr><td class="label"><b>TOTAL STRATÉGIE</b></td><td class="val {cl_cumul} big">{f(pnl_cumul,0,True)} $</td></tr>
  </table>
</div>

<!-- MATRICE -->
<div class="card" style="grid-column: 1 / -1">
  <h2>🎯 Sensibilité ±5%  <span style="font-weight:400;color:#484f58">Hedge depuis VWAP ${f(pos.get("hedge_avg_entry",0),0)} · Option cappée prime au-delà de +4%</span></h2>
  <table class="matrix">
    <tr>
      <th>Move</th><th>Spot</th><th>Delta</th>
      <th>PnL Option</th><th>PnL Hedge</th><th>PnL NET</th>
    </tr>
"""
    for (pct, ns, nd, po, ph, pn) in matrix_rows:
        is_zero = pct == 0
        rc      = "zero" if is_zero else ""
        co      = color(po)
        ch      = color(ph)
        cn      = color(pn)
        capped  = " 🔒" if pct >= 5 else ""
        html += f"""    <tr class="{rc}">
      <td>{'<b>' if is_zero else ''}{'+' if pct>0 else ''}{pct}%{'</b>' if is_zero else ''}</td>
      <td>${f(ns,0)}</td>
      <td>{f(nd,1)}%</td>
      <td class="{co}">{f(po,0,True)}${capped}</td>
      <td class="{ch}">{f(ph,0,True)}$</td>
      <td class="{cn}"><b>{f(pn,0,True)}$</b></td>
    </tr>
"""
    html += "  </table>\n</div>\n"

    # ── Historique rebalancements hedge ──────────────────────────────────────
    hedge_hist = pos.get("hedge_history", [])
    html += f"""
<!-- HEDGE HISTORY -->
<div class="card" style="grid-column: 1 / -1">
  <h2>🔄 Historique Hedge — {len(hedge_hist)} exécution(s)  <span style="font-weight:400;color:#484f58">VWAP actuel ${f(pos.get("hedge_avg_entry",0),2)} · Qty {pos.get("hedge_qty",0)} BTC</span></h2>
  <table class="matrix">
    <tr>
      <th style="text-align:left">Date / Heure</th>
      <th>Côté</th>
      <th>Qty ordre</th>
      <th>Spot</th>
      <th>Qty avant</th>
      <th>Qty après</th>
      <th>VWAP avant</th>
      <th>VWAP après</th>
      <th>Drift</th>
      <th style="text-align:left">Note</th>
    </tr>
"""
    for h in hedge_hist:
        side    = h.get("side", "?")
        side_cl = "neg" if side == "SELL" else "pos"
        qty_ord = h.get("qty", 0)
        html += f"""    <tr>
      <td style="text-align:left;color:#8b949e;font-size:0.8rem">{to_ny(h.get("ts",""))}</td>
      <td class="{side_cl}"><b>{side}</b></td>
      <td class="{side_cl}">{f(qty_ord,5,True)} BTC</td>
      <td>${f(h.get("spot",0),0)}</td>
      <td style="color:#8b949e">{f(h.get("qty_before",0),5)} BTC</td>
      <td><b>{f(h.get("qty_after",0),5)} BTC</b></td>
      <td style="color:#8b949e">${f(h.get("vwap_before",0),2)}</td>
      <td><b>${f(h.get("vwap_after",0),2)}</b></td>
      <td style="color:#8b949e">{f(h.get("drift",0),4,True)}</td>
      <td style="text-align:left;color:#484f58;font-size:0.78rem">{h.get("note","")}</td>
    </tr>
"""
    html += "  </table>\n</div>\n"

    # Alertes
    tte   = float(s.get("tte_days", 99))
    drft  = abs(float(s.get("hedge_delta_drift", 0)))
    div   = attr.get("delta_iv", 0)
    loss  = float(s.get("total_pnl_usd", 0))
    prem  = float(pos.get("entry_price", 0)) * float(pos.get("entry_spot", 1))

    alerts = []
    if tte <= 1:    alerts.append(('neg', '⚠️ ROLLER MAINTENANT', f'TTE = {f(tte,2)}j'))
    else:           alerts.append(('ok',  '✅ Roll OK',           f'TTE = {f(tte,2)}j'))
    if drft > hedge_thr_btc: alerts.append(('neg', '⚠️ REBALANCER',  f'Drift = {f(drft*100,2)}% > seuil {f(hedge_thr_pct,1)}% (IV-adj)'))
    else:                    alerts.append(('ok',  '✅ Hedge OK',    f'Drift = {f(drft*100,2)}% < seuil {f(hedge_thr_pct,1)}% (IV-adj)'))
    if div > 10:    alerts.append(('neg', '🚨 IV SPIKE',           f'ΔIV = {f(div,1,True)}pts'))
    elif div > 5:   alerts.append(('warn','⚠️ IV élevée',          f'ΔIV = {f(div,1,True)}pts'))
    else:           alerts.append(('ok',  '✅ IV OK',              f'ΔIV = {f(div,1,True)}pts'))
    if loss < -prem: alerts.append(('neg','🚨 STOP-LOSS',          f'Perte {f(loss,0,True)}$'))
    else:            alerts.append(('ok', '✅ Stop OK',            f'Perte {f(loss,0,True)}$ / prime {f(prem,0)}$'))

    html += '<div class="card alert-card" style="grid-column: 1 / -1"><h2>🚨 Alertes</h2><table>'
    for (cl, label, detail) in alerts:
        html += f'<tr><td class="val {cl}" style="width:30%">{label}</td><td style="color:#8b949e">{detail}</td></tr>'
    html += "</table></div>\n"
    html += "</div>\n"   # ← ferme la grille principale ici

# ── Graphiques historiques (hors grille, pleine largeur) ─────────────────────
if pnl_history:
    import json as _json

    labels     = [to_ny(p["ts"])[:16] for p in pnl_history]
    delta_data    = [p.get("delta_pct", 0)     for p in pnl_history]
    net_delta_data= [p.get("net_delta_pct", 0) for p in pnl_history]
    gamma_data    = [p.get("gamma_pts", 0)     for p in pnl_history]
    iv_data    = [p.get("iv_pct", 0)     for p in pnl_history]
    pnl_opt    = [p.get("pnl_option", 0) for p in pnl_history]
    pnl_hdg    = [p.get("pnl_hedge", 0)  for p in pnl_history]
    pnl_tot    = [p.get("pnl_total", 0)  for p in pnl_history]

    labels_js     = _json.dumps(labels)
    delta_js      = _json.dumps(delta_data)
    net_delta_js  = _json.dumps(net_delta_data)
    gamma_js      = _json.dumps(gamma_data)
    iv_js         = _json.dumps(iv_data)
    pnl_opt_js    = _json.dumps(pnl_opt)
    pnl_hdg_js    = _json.dumps(pnl_hdg)
    pnl_tot_js    = _json.dumps(pnl_tot)

    n_pts = len(pnl_history)

    spot_js = _json.dumps([p.get("spot", 0) for p in pnl_history])

    html += f"""
<!-- GRAPHIQUES -->
<div class="chart-section">

<div class="chart-card">
  <h2>📈 Greeks &amp; Spot — {n_pts} snapshots  <span style="font-weight:400;color:#484f58">Delta pos · Delta net · Gamma · Spot BTC</span></h2>
  <div class="chart-wrap"><canvas id="chartGreeks"></canvas></div>
</div>

<div class="chart-card">
  <h2>💰 PnL &amp; Spot — dans le temps  <span style="font-weight:400;color:#484f58">Option · Hedge · Total · Spot BTC</span></h2>
  <div class="chart-wrap"><canvas id="chartPnl"></canvas></div>
</div>

</div>

<script>
const LABELS = {labels_js};
const PT_R   = LABELS.length > 50 ? 0 : 3;
const TOOLTIP_DEFAULTS = {{
  backgroundColor: "#161b22", borderColor: "#30363d", borderWidth: 1,
  titleColor: "#e6edf3", bodyColor: "#8b949e",
}};

// ── Chart 1 : Delta + Gamma + Spot ────────────────────────────────────────
new Chart(document.getElementById("chartGreeks"), {{
  type: "line",
  data: {{
    labels: LABELS,
    datasets: [
      {{
        label: "Delta pos (%)",
        data: {delta_js},
        borderColor: "#58a6ff",
        backgroundColor: "rgba(88,166,255,0.07)",
        yAxisID: "yDelta",
        tension: 0.3, pointRadius: PT_R, borderWidth: 2, fill: true,
      }},
      {{
        label: "Delta net pos+hedge (%)",
        data: {net_delta_js},
        borderColor: "#a371f7",
        backgroundColor: "rgba(163,113,247,0.07)",
        yAxisID: "yDelta",
        tension: 0.3, pointRadius: PT_R, borderWidth: 2, borderDash: [4,2], fill: false,
      }},
      {{
        label: "Gamma (pts/1%)",
        data: {gamma_js},
        borderColor: "#f85149",
        backgroundColor: "transparent",
        yAxisID: "yGamma",
        tension: 0.3, pointRadius: PT_R, borderWidth: 2, borderDash: [5,3],
      }},
      {{
        label: "Spot BTC ($)",
        data: {spot_js},
        borderColor: "rgba(210,153,34,0.7)",
        backgroundColor: "transparent",
        yAxisID: "ySpot",
        tension: 0.3, pointRadius: 0, borderWidth: 1.5, borderDash: [2,4],
      }},
    ]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    interaction: {{ mode: "index", intersect: false }},
    plugins: {{
      legend: {{ labels: {{ color: "#8b949e", font: {{ size: 11 }} }} }},
      tooltip: {{ ...TOOLTIP_DEFAULTS,
        callbacks: {{
          label: ctx => {{
            const v = ctx.parsed.y;
            if (ctx.dataset.yAxisID === "ySpot") return ` Spot: $` + v.toLocaleString("en-US", {{maximumFractionDigits:0}});
            if (ctx.dataset.yAxisID === "yGamma") return ` Gamma: ` + v.toFixed(3) + ` pts`;
            if (ctx.dataset.label.includes("net")) return ` Delta net: ` + (v>=0?"+":"") + v.toFixed(2) + `%`;
            return ` Delta pos: ` + v.toFixed(2) + `%`;
          }}
        }}
      }},
    }},
    scales: {{
      x: {{ ticks: {{ color: "#484f58", maxTicksLimit: 14, maxRotation: 30, font: {{ size: 10 }} }}, grid: {{ color: "#21262d" }} }},
      yDelta: {{
        type: "linear", position: "left",
        title: {{ display: true, text: "Delta (%)", color: "#58a6ff", font: {{ size: 10 }} }},
        ticks: {{ color: "#58a6ff", font: {{ size: 10 }}, callback: v => v.toFixed(1)+"%" }},
        grid: {{ color: "#21262d" }},
      }},
      yGamma: {{
        type: "linear", position: "right",
        title: {{ display: true, text: "Gamma (pts)", color: "#f85149", font: {{ size: 10 }} }},
        ticks: {{ color: "#f85149", font: {{ size: 10 }}, callback: v => v.toFixed(2) }},
        grid: {{ drawOnChartArea: false }},
      }},
      ySpot: {{
        type: "linear", position: "right",
        title: {{ display: true, text: "Spot ($)", color: "#d29922", font: {{ size: 10 }} }},
        ticks: {{ color: "#d29922", font: {{ size: 10 }}, callback: v => "$"+Math.round(v/1000)+"k" }},
        grid: {{ drawOnChartArea: false }},
        offset: true,
      }},
    }}
  }}
}});

// ── Chart 2 : PnL + Spot ─────────────────────────────────────────────────
new Chart(document.getElementById("chartPnl"), {{
  type: "line",
  data: {{
    labels: LABELS,
    datasets: [
      {{
        label: "PnL Option ($)",
        data: {pnl_opt_js},
        borderColor: "#3fb950",
        backgroundColor: "rgba(63,185,80,0.07)",
        yAxisID: "yPnl",
        tension: 0.3, pointRadius: PT_R, borderWidth: 2, fill: true,
      }},
      {{
        label: "PnL Hedge ($)",
        data: {pnl_hdg_js},
        borderColor: "#ff9800",
        backgroundColor: "transparent",
        yAxisID: "yPnl",
        tension: 0.3, pointRadius: PT_R, borderWidth: 2, borderDash: [5,3],
      }},
      {{
        label: "PnL Total ($)",
        data: {pnl_tot_js},
        borderColor: "#e6edf3",
        backgroundColor: "rgba(230,237,243,0.04)",
        yAxisID: "yPnl",
        tension: 0.3, pointRadius: PT_R, borderWidth: 2.5, fill: true,
      }},
      {{
        label: "Spot BTC ($)",
        data: {spot_js},
        borderColor: "rgba(210,153,34,0.7)",
        backgroundColor: "transparent",
        yAxisID: "ySpot2",
        tension: 0.3, pointRadius: 0, borderWidth: 1.5, borderDash: [2,4],
      }},
    ]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    interaction: {{ mode: "index", intersect: false }},
    plugins: {{
      legend: {{ labels: {{ color: "#8b949e", font: {{ size: 11 }} }} }},
      tooltip: {{ ...TOOLTIP_DEFAULTS,
        callbacks: {{
          label: ctx => {{
            const v = ctx.parsed.y;
            if (ctx.dataset.yAxisID === "ySpot2") return ` Spot: $` + v.toLocaleString("en-US", {{maximumFractionDigits:0}});
            return ` ` + ctx.dataset.label + `: ` + (v >= 0 ? "+" : "") + v.toFixed(0) + `$`;
          }}
        }}
      }},
    }},
    scales: {{
      x: {{ ticks: {{ color: "#484f58", maxTicksLimit: 14, maxRotation: 30, font: {{ size: 10 }} }}, grid: {{ color: "#21262d" }} }},
      yPnl: {{
        type: "linear", position: "left",
        title: {{ display: true, text: "PnL ($)", color: "#8b949e", font: {{ size: 10 }} }},
        ticks: {{ color: "#8b949e", font: {{ size: 10 }}, callback: v => (v>=0?"+":"")+v.toFixed(0)+"$" }},
        grid: {{ color: "#21262d" }},
      }},
      ySpot2: {{
        type: "linear", position: "right",
        title: {{ display: true, text: "Spot ($)", color: "#d29922", font: {{ size: 10 }} }},
        ticks: {{ color: "#d29922", font: {{ size: 10 }}, callback: v => "$"+Math.round(v/1000)+"k" }},
        grid: {{ drawOnChartArea: false }},
        offset: true,
      }},
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
