"""Sweep des seuils du circuit breaker."""
import io, contextlib
import backtest
from backtest import run

def stats(ec):
    eq = [e[1] for e in ec]
    peak, max_dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        max_dd = max(max_dd, peak - v)
    rets = [eq[i] - eq[i-1] for i in range(1, len(eq))]
    m = sum(rets)/len(rets)
    s = (sum((r-m)**2 for r in rets)/len(rets)) ** 0.5
    return eq[-1], max_dd, (m/s*(365**0.5) if s > 0 else 0)

print(f"{'move3j>':>8} {'dvol3j>':>8} {'PnL':>9} {'MaxDD':>8} {'Sharpe':>7} {'PnL/DD':>7} {'triggers':>9}")
for mv, dv in [(8, 10), (10, 12), (12, 15), (15, 18)]:
    backtest.CB_MOVE_3D_PCT = mv
    backtest.CB_DVOL_3D_PTS = dv
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ec = run(4.0, always_one=True, circuit_breaker=True)
    pnl, dd, sh = stats(ec)
    trig = "?"
    for line in buf.getvalue().splitlines():
        if "Circuit breaker" in line:
            trig = line.split(":")[1].split("déclenchements")[0].strip()
    print(f"{mv:>7}% {dv:>6}pt {pnl:>8,.0f}$ {dd:>7,.0f}$ {sh:>7.2f} {pnl/max(dd,1):>7.2f} {trig:>9}")

# baseline
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ec = run(4.0, always_one=True, circuit_breaker=False)
pnl, dd, sh = stats(ec)
print(f"{'—':>8} {'—':>8} {pnl:>8,.0f}$ {dd:>7,.0f}$ {sh:>7.2f} {pnl/max(dd,1):>7.2f} {'(sans CB)':>9}")
