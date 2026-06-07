"""
generate_html.py — Génère docs/index.html depuis pnl_summary.json + positions.json
Appelé par GitHub Actions après chaque snapshot.
"""
import json, math
from pathlib import Path
from datetime import datetime, timezone

# ── Lecture des données ────────────────────────────────────────────────────────
pos_raw  = json.loads(Path("positions.json").read_text())
summ_raw = json.loads(Path("pnl_summary.json").read_text())

pos   = pos_raw.get("open")
hist  = pos_raw.get("history", [])

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
        nd_pct    = max(0.0, delta_pct - gamma_pts * abs(pct))
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
ts = summ_raw.get("timestamp", "—")
generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

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
<title>VRP Monitor — {pos["instrument_name"] if pos else "Aucune position"}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'SF Mono', 'Fira Code', monospace; background: #0d1117; color: #e6edf3; min-height: 100vh; padding: 24px; }}
  h1 {{ font-size: 1.4rem; color: #58a6ff; margin-bottom: 4px; }}
  .subtitle {{ color: #8b949e; font-size: 0.82rem; margin-bottom: 24px; }}
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

    html += f"""
<h1>📊 {instrument}</h1>
<div class="subtitle">Données : {ts} · Généré : {generated} · ↻ auto-refresh 5min</div>

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
    {row("Date d'entrée", str(pos.get("entry_ts","—")))}
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
    {row("Seuil rebal. (IV-adj)", f'<span class="neu">{f(hedge_thr_pct,1)}% delta = {f(hedge_thr_btc,4)} BTC</span>')}
    {row("Drift Δ", f'<span class="{"warn" if abs(float(s.get("hedge_delta_drift",0)))>hedge_thr_btc else "ok"}">{f(s.get("hedge_delta_drift"),4,True)} ({f(abs(float(s.get("hedge_delta_drift",0)))*100,2)}%)</span>  {"⚠️ REBALANCER" if abs(float(s.get("hedge_delta_drift",0)))>hedge_thr_btc else "✅ OK"}')}
  </table>
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
    {row("VRP capture", f'{s.get("vrp_capture_pct") or "(< 6h)"}%')}
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
      <td style="text-align:left;color:#8b949e;font-size:0.8rem">{str(h.get("ts",""))}</td>
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

html += f"""
</div>
<footer>VRP Monitor · Généré le {generated} · GitHub Actions · Auto-refresh 5 min</footer>
</body></html>
"""

Path("docs").mkdir(exist_ok=True)
Path("docs/index.html").write_text(html, encoding="utf-8")
print(f"docs/index.html généré ({len(html)} chars)")
