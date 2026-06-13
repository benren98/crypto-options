import io, contextlib
from backtest import run

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ec = run(4.0, always_one=True)

eq = [(d, e) for d, e, *_ in ec]
eqd = dict(eq)
peak_v, peak_d = eq[0][1], eq[0][0]
max_dd, dd_peak_d, dd_trough_d = 0, None, None
for d, v in eq:
    if v > peak_v:
        peak_v, peak_d = v, d
    dd = peak_v - v
    if dd > max_dd:
        max_dd, dd_peak_d, dd_trough_d = dd, peak_d, d

print(f"Max DD : {max_dd:,.0f} $")
print(f"Pic    : {dd_peak_d} (equity {eqd[dd_peak_d]:,.0f} $)")
print(f"Creux  : {dd_trough_d} (equity {eqd[dd_trough_d]:,.0f} $)")
rec = None
target = eqd[dd_peak_d]
for d, v in eq:
    if d > dd_trough_d and v >= target:
        rec = d
        break
print(f"Recouvre le pic : {rec if rec else 'jamais (fin de periode)'}")

# top 5 drawdowns distincts
print("\nTop 5 drawdowns :")
dds = []
peak_v, peak_d, trough_v, trough_d, in_dd = None, None, None, None, False
for d, v in eq:
    if peak_v is None or v >= peak_v:
        if in_dd:
            dds.append((peak_v - trough_v, peak_d, trough_d, d))
            in_dd = False
        peak_v, peak_d = v, d
    else:
        if not in_dd or v < trough_v:
            trough_v, trough_d = v, d
            in_dd = True
if in_dd:
    dds.append((peak_v - trough_v, peak_d, trough_d, None))
dds.sort(reverse=True)
for amt, pd_, td_, rd_ in dds[:5]:
    print(f"  -{amt:>7,.0f} $   pic {pd_}  creux {td_}  recouvre {rd_ or 'jamais'}")
