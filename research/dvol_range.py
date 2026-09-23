import sys; sys.path.insert(0, '.')
from greeks_hedge import get, now_ms, fetch_hv
from datetime import datetime, timezone

end_ts   = now_ms()
start_ts = end_ts - 30 * 24 * 3600 * 1000
data = get('get_volatility_index_data', {'currency':'BTC','start_timestamp':start_ts,'end_timestamp':end_ts,'resolution':'1D'})
rows = data.get('data', [])

hv = fetch_hv('BTC', days=10)
print(f"HV 10j actuelle : {hv:.1f}%")
print()
print(f"{'Date':<12} {'DVOL':>8} {'IV/HV':>7}")
print("-" * 30)
for r in rows:
    dt    = datetime.fromtimestamp(r[0]/1000, tz=timezone.utc).strftime('%Y-%m-%d')
    dvol  = r[4]
    ratio = dvol / hv if hv > 0 else 0
    flag  = " <-- x2" if ratio >= 2.0 else (" <-- x1.5" if ratio >= 1.5 else "")
    print(f"{dt:<12} {dvol:>7.1f}%  {ratio:>6.2f}x{flag}")

closes = [r[4] for r in rows]
print()
print(f"Min DVOL : {min(closes):.1f}%   Max DVOL : {max(closes):.1f}%")
print(f"Ratio min: {min(closes)/hv:.2f}x   Ratio max: {max(closes)/hv:.2f}x  (vs HV {hv:.1f}%)")
