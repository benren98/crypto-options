"""Rendement sur capital mobilisé (cas équilibré = 20% du notionnel puts)."""
import io, contextlib
import backtest as bt

CAP_PCT = 0.20   # capital équilibré = 20% du notionnel puts

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    bt.run(4.0, always_one=True, circuit_breaker=True)   # params actuels + CB
r = bt._LAST_RUN
curve = r["curve"]; notu = r["notionals_usd"]

days = len(curve)
years = days / 365
eq = [c[1] for c in curve]
total_pnl = eq[-1] - eq[0]
annual_pnl = total_pnl / years

# Capital mobilisé jour par jour = 20% du notionnel $ (avec un plancher : capital ne
# descend pas sous 20% d'au moins un petit book quand peu de positions)
caps = [CAP_PCT * n for n in notu if n > 0]
avg_cap  = sum(caps) / len(caps)
peak_cap = max(CAP_PCT * n for n in notu)
# Capital fixe réaliste = dimensionné pour le pic (ce qu'on immobilise vraiment)
roc_avg  = annual_pnl / avg_cap * 100
roc_peak = annual_pnl / peak_cap * 100

# Drawdown en $ et en % du capital fixe (pic)
peak, max_dd = eq[0], 0.0
for v in eq:
    peak = max(peak, v); max_dd = max(max_dd, peak - v)

print(f"Periode            : {curve[0][0]} -> {curve[-1][0]}  ({years:.2f} ans)")
print(f"PnL total          : {total_pnl:>10,.0f} $")
print(f"PnL annualise      : {annual_pnl:>10,.0f} $/an")
print()
print(f"Notionnel puts moyen : {sum(n for n in notu if n>0)/len([n for n in notu if n>0]):>10,.0f} $")
print(f"Notionnel puts pic   : {max(notu):>10,.0f} $")
print()
print(f"Capital equilibre (20% notionnel) :")
print(f"  moyen mobilise   : {avg_cap:>10,.0f} $")
print(f"  pic (capital fixe a immobiliser) : {peak_cap:>10,.0f} $")
print()
print(f"=== RENDEMENT SUR CAPITAL ===")
print(f"  sur capital moyen mobilise : {roc_avg:>6.1f}% / an")
print(f"  sur capital fixe (pic)     : {roc_peak:>6.1f}% / an")
print()
print(f"Max drawdown : {max_dd:,.0f} $  =  {max_dd/peak_cap*100:.0f}% du capital fixe")
