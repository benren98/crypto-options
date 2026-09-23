"""Ratio DVOL / HV_blend quotidien sur 30j — calibration du composant s_iv_hv."""
import sys; sys.path.insert(0, '.')
from greeks_hedge import get, now_ms
from datetime import datetime, timezone
import math

end_ts   = now_ms()
start_ts = end_ts - 65 * 24 * 3600 * 1000

# Prix daily pour HV glissantes
cd = get('get_tradingview_chart_data', {
    'instrument_name': 'BTC-PERPETUAL',
    'start_timestamp': start_ts, 'end_timestamp': end_ts, 'resolution': '1D'})
closes, ticks = cd['close'], cd['ticks']

def hv_window(i, n):
    w = closes[i-n:i+1]
    rets = [math.log(w[j]/w[j-1]) for j in range(1, len(w))]
    return math.sqrt(sum(r*r for r in rets)/len(rets)) * math.sqrt(365) * 100

# DVOL daily
dv = get('get_volatility_index_data', {'currency':'BTC','start_timestamp':start_ts,'end_timestamp':end_ts,'resolution':'1D'})
dvol_by_day = {datetime.fromtimestamp(r[0]/1000, tz=timezone.utc).strftime('%Y-%m-%d'): r[4] for r in dv['data']}

print(f"{'Date':<12} {'HV10':>6} {'HV30':>6} {'Blend':>6} {'DVOL':>6} {'Ratio':>6} {'s_iv_hv':>8}")
ratios = []
for i in range(30, len(closes)):
    dt = datetime.fromtimestamp(ticks[i]/1000, tz=timezone.utc).strftime('%Y-%m-%d')
    if dt not in dvol_by_day:
        continue
    h10, h30 = hv_window(i, 10), hv_window(i, 30)
    blend = 0.5*h10 + 0.5*h30
    dvol = dvol_by_day[dt]
    ratio = dvol/blend
    s = max(0.0, min(1.0, ratio - 1.0))
    ratios.append(ratio)
    print(f"{dt:<12} {h10:>5.1f}% {h30:>5.1f}% {blend:>5.1f}% {dvol:>5.1f}% {ratio:>5.2f}x {s:>8.3f}")

rs = sorted(ratios)
print()
print(f"Ratio min {min(ratios):.2f}x | mediane {rs[len(rs)//2]:.2f}x | P90 {rs[int(0.9*len(rs))]:.2f}x | max {max(ratios):.2f}x")
print(f"s_iv_hv actuel (ratio-1) : mediane {max(0,rs[len(rs)//2]-1):.3f} | max {max(0,max(ratios)-1):.3f}")
