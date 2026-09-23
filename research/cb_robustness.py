"""
cb_robustness.py — Robustesse du CB gradué gagnant (−5/+6 keep.3 rest3).

(1) BTC : PnL par année civile, baseline binaire vs gradué → le bénéfice est-il
    réparti ou dû à un seul événement (4 août 2024) ?
(2) ETH : même config rejouée sur l'historique ETH (données injectées) → le levier
    tient-il hors-échantillon sur un autre sous-jacent ?
"""
import sys, io, contextlib
sys.path.insert(0, '.')
import backtest as bt
import backtest_cb as cb
import backtest_eth as be

WIN = dict(t1_move=5.0, t1_dvol=6.0, keep=0.3, t1_restore=3.0)

def line(label, r):
    print(f"  {label:<22} PnL {r['pnl']:>8,.0f}$ · MaxDD {r['maxdd']:>7,.0f}$ · "
          f"Calmar {r['calmar']:>5.2f} · pireJ {r['worst']:>7,.0f}$")

# ── (1) BTC : baseline vs gradué + découpage annuel ──
btc_days = bt.fetch_history(4.15)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    base = cb.run_cb(days=btc_days)                       # binaire
    grad = cb.run_cb(days=btc_days, **WIN)                # gradué
print(f"\n{'='*68}\n  (1) BTC — baseline binaire vs CB gradué (−5/+6 keep.3 rest3)\n{'='*68}")
line("Baseline binaire", base)
line("CB gradué", grad)
print(f"\n  PnL par année civile :")
print(f"  {'année':<8} {'binaire':>12} {'gradué':>12} {'Δ':>10}")
for y in sorted(base['annual']):
    b = base['annual'][y]; g = grad['annual'].get(y, 0)
    print(f"  {y:<8} {b:>11,.0f}$ {g:>11,.0f}$ {g-b:>9,.0f}$")

# ── (2) ETH : même config sur données ETH ──
eth_raw = be.fetch_history(4.15)
eth_days = [{'date': d['date'], 'spot': d['spot'], 'dvol': d['dvol']} for d in eth_raw]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    base_e = cb.run_cb(days=eth_days)
    grad_e = cb.run_cb(days=eth_days, **WIN)
print(f"\n{'='*68}\n  (2) ETH — baseline binaire vs CB gradué (même config)\n{'='*68}")
line("ETH baseline binaire", base_e)
line("ETH CB gradué", grad_e)
print(f"\n  (ETH : skew/pricing calibrés BTC → sanity check directionnel, pas un chiffre absolu)")
