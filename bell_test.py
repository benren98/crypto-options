"""Compare sizing rank linéaire vs profil en cloche."""
import io, contextlib
from backtest import run, rank_mult_linear, rank_mult_bell

def stats(ec):
    eq = [e[1] for e in ec]
    peak, max_dd = eq[0], 0.0
    dd_dates = (None, None)
    peak_d = ec[0][0]
    for d, v, *_ in ec:
        if v > peak:
            peak, peak_d = v, d
        if peak - v > max_dd:
            max_dd = peak - v
            dd_dates = (peak_d, d)
    rets = [eq[i] - eq[i-1] for i in range(1, len(eq))]
    m = sum(rets)/len(rets)
    s = (sum((r-m)**2 for r in rets)/len(rets)) ** 0.5
    sharpe = m/s*(365**0.5) if s > 0 else 0
    return eq[-1], max_dd, sharpe, dd_dates

results = {}
for name, rm in [("Lineaire (actuel)", rank_mult_linear), ("Cloche (pic 0.65)", rank_mult_bell)]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ec = run(4.0, always_one=True, rank_mult=rm)
    results[name] = stats(ec)

print(f"{'Profil':<20} {'PnL final':>10} {'Max DD':>9} {'Sharpe':>7} {'PnL/DD':>7}  Periode du max DD")
for name, (pnl, dd, sh, (d1, d2)) in results.items():
    print(f"{name:<20} {pnl:>9,.0f}$ {dd:>8,.0f}$ {sh:>7.2f} {pnl/dd:>7.2f}  {d1} -> {d2}")
