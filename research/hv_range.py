import sys; sys.path.insert(0, '.')
from greeks_hedge import get, now_ms
from datetime import datetime, timezone
import math

# Prix daily BTC sur 45j pour calculer HV 10j glissante
end_ts   = now_ms()
start_ts = end_ts - 45 * 24 * 3600 * 1000
data = get('get_tradingview_chart_data', {
    'instrument_name': 'BTC-PERPETUAL',
    'start_timestamp': start_ts,
    'end_timestamp':   end_ts,
    'resolution':      '1D',
})
closes = data.get('close', [])
ticks  = data.get('ticks', [])

# HV 10j glissante : écart-type des 10 derniers log-returns, annualisé
results = []
for i in range(10, len(closes)):
    window = closes[i-10:i+1]
    rets   = [math.log(window[j]/window[j-1]) for j in range(1, len(window))]
    hv     = math.sqrt(sum(r**2 for r in rets) / len(rets)) * math.sqrt(365) * 100
    dt     = datetime.fromtimestamp(ticks[i]/1000, tz=timezone.utc).strftime('%Y-%m-%d')
    results.append((dt, hv))

# Garder les 30 derniers jours
results = results[-30:]
print(f"{'Date':<12} {'HV 10j':>8}")
print("-" * 22)
for dt, hv in results:
    print(f"{dt:<12} {hv:>7.1f}%")

hvs = [r[1] for r in results]
print(f"\nMin : {min(hvs):.1f}%   Max : {max(hvs):.1f}%   Actuel : {hvs[-1]:.1f}%")
