"""Compare anciens vs nouveaux paramètres, avec le solveur strike_for_delta corrigé."""
import io, contextlib
import backtest as bt

def stats(ec):
    eq = [e[1] for e in ec]
    peak, max_dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v); max_dd = max(max_dd, peak - v)
    rets = [eq[i]-eq[i-1] for i in range(1, len(eq))]
    m = sum(rets)/len(rets); s = (sum((r-m)**2 for r in rets)/len(rets))**0.5
    return eq[-1], max_dd, (m/s*(365**0.5) if s>0 else 0)

CONFIGS = {
    "ANCIENS (delta -.12..-.25, TTE 7/14/21, prime 0$)":
        ([-0.12,-0.16,-0.20,-0.25], [7,14,21], 0.0),
    "NOUVEAUX (delta -.05..-.25, TTE 3/7/14/21, prime 50$)":
        ([-0.05,-0.08,-0.12,-0.16,-0.20,-0.25], [3,7,14,21], 50.0),
}

for cb in (False, True):
    print(f"\n{'='*78}\n  CIRCUIT BREAKER : {'ON' if cb else 'OFF'}\n{'='*78}")
    print(f"  {'Config':<52} {'PnL':>8} {'MaxDD':>8} {'Sharpe':>7} {'Trades':>7}")
    for label,(dt,tte,prem) in CONFIGS.items():
        bt.DELTA_TARGETS, bt.TTE_CHOICES, bt.MIN_PREMIUM_USD = dt, tte, prem
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ec = bt.run(4.0, always_one=True, circuit_breaker=cb)
        pnl, dd, sh = stats(ec)
        ntr = [l for l in buf.getvalue().splitlines() if "Trades" in l][0].split(":")[1].split("|")[0].strip()
        print(f"  {label:<52} {pnl:>7,.0f}$ {dd:>7,.0f}$ {sh:>7.2f} {ntr:>7}")
