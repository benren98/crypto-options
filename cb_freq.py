"""Fréquence historique des déclencheurs du circuit breaker."""
import sys; sys.path.insert(0, '.')
from greeks_hedge import get, now_ms
from datetime import datetime, timezone

end_ts   = now_ms()
start_ts = end_ts - 5 * 365 * 24 * 3600 * 1000

dv = get('get_volatility_index_data', {'currency': 'BTC',
     'start_timestamp': start_ts, 'end_timestamp': end_ts, 'resolution': '1D'})
dvol = [(datetime.fromtimestamp(r[0]/1000, tz=timezone.utc).date(), r[4]) for r in dv['data'] if r[4]]

cd = get('get_tradingview_chart_data', {'instrument_name': 'BTC-PERPETUAL',
     'start_timestamp': start_ts, 'end_timestamp': end_ts, 'resolution': '1D'})
spot = {datetime.fromtimestamp(t/1000, tz=timezone.utc).date(): c
        for t, c in zip(cd['ticks'], cd['close']) if c}

print(f"Historique DVOL : {dvol[0][0]} -> {dvol[-1][0]} ({len(dvol)} jours)")

trig_dvol, trig_move, trig_both = [], [], []
for i in range(3, len(dvol)):
    d, v = dvol[i]
    chg = v - dvol[i-3][1]
    s_now, s_ref = spot.get(d), spot.get(dvol[i-3][0])
    mv = abs(s_now / s_ref - 1) * 100 if s_now and s_ref else 0
    hit_d = chg > 12
    hit_m = mv > 10
    if hit_d:
        trig_dvol.append((d, chg, mv))
    if hit_m:
        trig_move.append((d, chg, mv))
    if hit_d and not hit_m:
        trig_both.append((d, chg, mv))

n_days = len(dvol) - 3
years = n_days / 365
print(f"\nJours DVOL 3j > +12pts : {len(trig_dvol)}  (~{len(trig_dvol)/years:.1f}/an)")
print(f"Jours |move 3j| > 10%  : {len(trig_move)}  (~{len(trig_move)/years:.1f}/an)")
print(f"DVOL seul (sans move>10%) : {len(trig_both)}  (~{len(trig_both)/years:.1f}/an)")

# Episodes distincts (jours consécutifs regroupés)
def episodes(days):
    eps, prev = [], None
    for d, *_ in days:
        if prev is None or (d - prev).days > 5:
            eps.append(d)
        prev = d
    return eps

print(f"\nEpisodes distincts (>5j d'écart) :")
print(f"  DVOL +12pts : {len(episodes(trig_dvol))} épisodes -> {episodes(trig_dvol)}")
print(f"  Move >10%   : {len(episodes(trig_move))} épisodes")
print(f"\nDéclenchements DVOL-seul (le move n'aurait pas suffi) :")
for d, chg, mv in trig_both:
    print(f"  {d}  DVOL +{chg:.1f}pts  move {mv:.1f}%")
