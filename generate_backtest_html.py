"""
generate_backtest_html.py — Génère docs/backtest.html : dashboard des backtests +
surface de skew fittée, à partir de backtest_routine.json (produit par backtest_routine.py).

Sections : courbes de skew par maturité (réel fité vs modèle linéaire), courbe d'equity
du baseline, et tableaux des sweeps classés par sensibilité (paramètres à regarder).
"""
import json
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "backtest_routine.json"
OUT  = ROOT / "docs" / "backtest.html"


def _load():
    if not DATA.exists():
        return None
    return json.loads(DATA.read_text(encoding="utf-8"))


def _skew_chart(skew_fit):
    """Datasets Chart.js : 1 courbe par bucket (au régime de référence) + variante
    DVOL+20 en pointillé pour les buckets régime-aware + référence linéaire 0.013."""
    xs = list(range(0, 26))
    ds = []
    cols = ["#58a6ff", "#a371f7", "#3fb950"]
    if skew_fit and skew_fit.get("buckets"):
        for i, bk in enumerate(skew_fit["buckets"]):
            a0, b0 = bk.get("a0", 0.0), bk.get("b0", 0.0)
            col = cols[i % len(cols)]
            ys = [round(1 + a0*x + b0*x*x, 3) for x in xs]
            ds.append(f'{{label:"{bk["label"]} @DVOL {bk.get("dvol_ref","?")} (R²={bk.get("r2")})",'
                      f'data:{json.dumps(ys)},borderColor:"{col}",backgroundColor:"transparent",'
                      f'tension:0.3,pointRadius:0,borderWidth:2}}')
            if bk.get("regime_aware"):
                a1, b1 = bk.get("a1", 0.0), bk.get("b1", 0.0)
                yh = [round(1 + (a0+a1*20)*x + (b0+b1*20)*x*x, 3) for x in xs]
                ds.append(f'{{label:"{bk["label"]} @DVOL +20 (stress)",data:{json.dumps(yh)},'
                          f'borderColor:"{col}",backgroundColor:"transparent",tension:0.3,'
                          f'pointRadius:0,borderWidth:1.5,borderDash:[4,3]}}')
    lin = [round(1 + 0.013*x, 3) for x in xs]
    ds.append(f'{{label:"modèle linéaire (0.013)",data:{json.dumps(lin)},borderColor:"#f85149",'
              f'backgroundColor:"transparent",tension:0,pointRadius:0,borderWidth:1.5,borderDash:[6,4]}}')
    return json.dumps(xs), ",".join(ds)


def _equity_chart(base):
    curve = base.get("curve", [])
    labels = [c[0] for c in curve]
    data = [c[1] for c in curve]
    return json.dumps(labels), json.dumps(data)


def _verdict(s):
    if s["sensitivity"] < 0.5:                                    return ("· peu sensible", "low")
    if s.get("robust") and s.get("extend"):                       return ("✅↗ robuste · optimum au bord (étendre)", "ok")
    if s.get("robust"):                                           return ("✅ robuste (multi-régimes)", "ok")
    if (s.get("best_worst_fold") or -1) <= 0:                     return ("⛔ s'effondre ≥1 régime", "bad")
    if s.get("fold_wins", 0) < (s.get("n_folds", 5)+1)//2:        return ("⛔ un régime porte tout", "bad")
    if s.get("extend"):                                           return ("↗ à étendre (tendance au bord)", "warn")
    if not s.get("plateau"):                                      return ("⚠ pic isolé", "warn")
    return ("⚠ limite", "warn")


def _sweep_table(s):
    rows = ""
    for r in s["results"]:
        is_best = r.get("is_best", False)       # = recommandé (maximin multi-régimes)
        is_cur = r.get("is_current", False)
        style = ' style="'
        if is_best: style += 'background:#1f6f3f33;'
        if is_cur:  style += 'border-left:3px solid #58a6ff;'
        style += '"'
        tags = ""
        if is_cur:  tags += ' <span class="tag cur">actuel</span>'
        if is_best: tags += ' <span class="tag best">reco★</span>'
        def cl(c): return "pos" if (c or 0) >= 2 else ("warn" if (c or 0) >= 1 else "neg")
        wf = r.get("worst_fold"); mf = r.get("mean_fold")
        rows += (f'<tr{style}><td>{r["label"]}{tags}</td><td>{r["pnl"]:,}$</td>'
                 f'<td class="{cl(wf)}">{"—" if wf is None else wf}</td>'
                 f'<td class="{cl(mf)}">{"—" if mf is None else mf}</td>'
                 f'<td class="{cl(r["calmar"])}">{r["calmar"]}</td></tr>')
    sbadge = "high" if s["sensitivity"] >= 1.0 else "low"
    vtxt, vcl = _verdict(s)
    wins = f' · gagne {s.get("fold_wins","?")}/{s.get("n_folds","?")} folds' if s["sensitivity"] >= 0.5 else ""
    g = s.get("gain_vs_current")
    if s.get("current_is_best") or (g is not None and g <= 0.01):
        gain_html = '<span class="tag best">déjà optimal</span>'
    elif g is not None:
        gain_html = f'<span class="tag cur">gain +{g} vs actuel</span>'
    else:
        gain_html = ""
    return f"""
    <div class="card">
      <h3>{s['param']} {gain_html} <span class="sens {sbadge}">amplitude ΔCalmar {s['sensitivity']}</span>
          <span class="verdict {vcl}">{vtxt}</span><span class="muted">{wins}</span></h3>
      <table><thead><tr><th>config</th><th>PnL</th><th>pire fold</th><th>moy fold</th><th>full</th></tr></thead>
      <tbody>{rows}</tbody></table>
    </div>"""


def generate():
    d = _load()
    OUT.parent.mkdir(exist_ok=True)
    if not d:
        OUT.write_text("<h1>Pas encore de backtest_routine.json — lance backtest_routine.py</h1>",
                       encoding="utf-8")
        print("backtest.html : aucune donnée."); return

    b = d["baseline"]
    skew_x, skew_ds = _skew_chart(d.get("skew_fit"))
    eq_labels, eq_data = _equity_chart(b)
    sweeps_html = "".join(_sweep_table(s) for s in d["sweeps"])
    cov = (d.get("skew_fit") or {}).get("coverage") or {}
    cov_txt = f"{cov.get('days','?')}j ({cov.get('start','?')}→{cov.get('end','?')})" if cov else "skew linéaire (pas encore de surface réelle)"

    html = f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backtests & Skew</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
:root{{color-scheme:dark}}
body{{background:#0d1117;color:#e6edf3;font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:24px;max-width:1200px;margin:0 auto}}
h1{{font-size:1.4rem}} h2{{font-size:1.05rem;color:#8b949e;margin-top:28px}} h3{{font-size:0.9rem;margin:0 0 8px}}
a.back{{color:#58a6ff;text-decoration:none;font-size:0.85rem}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:14px}}
.card{{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:14px}}
.chart-wrap{{position:relative;height:300px}}
table{{width:100%;border-collapse:collapse;font-size:0.8rem}}
th,td{{text-align:right;padding:4px 6px;border-bottom:1px solid #21262d}} th:first-child,td:first-child{{text-align:left}}
.pos{{color:#3fb950}} .warn{{color:#d29922}} .neg{{color:#f85149}}
.kpi{{display:flex;gap:24px;flex-wrap:wrap;margin:10px 0}}
.kpi div{{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:10px 16px}}
.kpi b{{font-size:1.2rem}}
.sens{{font-size:0.7rem;padding:2px 6px;border-radius:4px;font-weight:400}}
.sens.high{{background:#f8514933;color:#f85149}} .sens.low{{background:#30363d;color:#8b949e}}
.tag{{font-size:0.62rem;padding:1px 5px;border-radius:3px}}
.tag.cur{{background:#1f6feb33;color:#58a6ff}} .tag.best{{background:#1f6f3f55;color:#3fb950}}
.tag.isb{{background:#30363d;color:#8b949e}}
.verdict{{font-size:0.66rem;padding:2px 6px;border-radius:4px;font-weight:400}}
.verdict.ok{{background:#1f6f3f55;color:#3fb950}} .verdict.bad{{background:#f8514933;color:#f85149}}
.verdict.warn{{background:#d2992233;color:#d29922}} .verdict.low{{background:#30363d;color:#8b949e}}
.muted{{color:#484f58;font-size:0.8rem}}
</style></head><body>
<a class="back" href="index.html">← Dashboard live</a>
<h1>📊 Backtests &amp; Surface de Skew</h1>
<p class="muted">Généré {d['generated_at']} · surface réelle : {cov_txt} · ⚠ provisoire tant que peu de jours collectés</p>

<div class="kpi">
  <div>PnL baseline<br><b class="pos">{b['pnl']:,}$</b></div>
  <div>MaxDD<br><b>{b['maxdd']:,}$</b></div>
  <div>Calmar<br><b>{b['calmar']}</b></div>
  <div>Sharpe<br><b>{b['sharpe']}</b></div>
  <div>Pire jour<br><b class="neg">{b['worst']:,}$</b></div>
  <div>Trades<br><b>{b.get('trades','-')}</b></div>
</div>

<div class="grid">
  <div class="card"><h3>Surface de skew — IV(K)/IV_ATM par maturité</h3>
    <div class="chart-wrap"><canvas id="skew"></canvas></div>
    <p class="muted">Le modèle linéaire (rouge pointillé) sous-estime le vrai skew convexe.</p></div>
  <div class="card"><h3>Courbe d'equity — config baseline (prod)</h3>
    <div class="chart-wrap"><canvas id="eq"></canvas></div></div>
</div>

<h2>Sweeps de paramètres — classés par sensibilité</h2>
<p class="muted">Anti-overfit : la période est découpée en <b>5 folds contigus</b> (≈ régimes de vol).
On ne juge pas sur la meilleure perf globale (biaisée par 2024) mais sur le <b>pire fold</b> (maximin)
et l'<b>accord du vainqueur entre folds</b>. <span class="tag best">reco★</span> = valeur recommandée
(meilleure moyenne sans s'effondrer dans aucun régime) · <span class="tag cur">actuel</span> config prod.
✅ robuste = gagne dans la majorité des régimes + plateau · ⛔ = un seul régime porte le résultat (overfit).
Règle : changer 1-2 params à la fois, confirmer sur ETH, ne jamais empiler tous les optima.</p>
<div class="grid">{sweeps_html}</div>

<script>
const TT={{backgroundColor:"#161b22",borderColor:"#30363d",borderWidth:1,titleColor:"#e6edf3",bodyColor:"#8b949e"}};
new Chart(document.getElementById("skew"),{{type:"line",data:{{labels:{skew_x},datasets:[{skew_ds}]}},
 options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{labels:{{color:"#8b949e",font:{{size:10}}}}}},tooltip:TT}},
 scales:{{x:{{title:{{display:true,text:"OTM %",color:"#8b949e"}},ticks:{{color:"#484f58"}},grid:{{color:"#21262d"}}}},
 y:{{title:{{display:true,text:"IV / IV_ATM",color:"#8b949e"}},ticks:{{color:"#484f58"}},grid:{{color:"#21262d"}}}}}}}}}});
new Chart(document.getElementById("eq"),{{type:"line",data:{{labels:{eq_labels},datasets:[
 {{label:"Equity ($)",data:{eq_data},borderColor:"#3fb950",backgroundColor:"rgba(63,185,80,0.08)",tension:0.3,pointRadius:0,borderWidth:2,fill:true}}]}},
 options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}},tooltip:TT}},
 scales:{{x:{{ticks:{{color:"#484f58",maxTicksLimit:10,font:{{size:9}}}},grid:{{color:"#21262d"}}}},
 y:{{ticks:{{color:"#484f58",callback:v=>"$"+Math.round(v/1000)+"k"}},grid:{{color:"#21262d"}}}}}}}}}});
</script></body></html>"""
    OUT.write_text(html, encoding="utf-8")
    print(f"backtest.html généré ({len(html):,} chars)")


if __name__ == "__main__":
    generate()
