"""Compare la stratégie avec et sans circuit breaker."""
import io, contextlib
from backtest import run

def stats(ec):
    eq = [e[1] for e in ec]
    peak, max_dd = eq[0], 0.0
    peak_d, dd_dates = ec[0][0], (None, None)
    for d, v, *_ in ec:
        if v > peak:
            peak, peak_d = v, d
        if peak - v > max_dd:
            max_dd, dd_dates = peak - v, (peak_d, d)
    rets = [eq[i] - eq[i-1] for i in range(1, len(eq))]
    m = sum(rets)/len(rets)
    s = (sum((r-m)**2 for r in rets)/len(rets)) ** 0.5
    return eq[-1], max_dd, (m/s*(365**0.5) if s > 0 else 0), dd_dates

print(f"{'Variante':<28} {'PnL final':>10} {'Max DD':>9} {'Sharpe':>7} {'PnL/DD':>7}  Max DD periode")
for name, kw in [("Sans circuit breaker", {}),
                 ("Avec circuit breaker", {"circuit_breaker": True})]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ec = run(4.0, always_one=True, **kw)
    pnl, dd, sh, (d1, d2) = stats(ec)
    print(f"{name:<28} {pnl:>9,.0f}$ {dd:>8,.0f}$ {sh:>7.2f} {pnl/max(dd,1):>7.2f}  {d1} -> {d2}")
    # extraire la ligne CB du rapport
    for line in buf.getvalue().splitlines():
        if "Circuit breaker" in line:
            print(f"  {line.strip()}")
