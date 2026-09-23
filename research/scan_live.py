import sys
sys.path.insert(0, '.')
from greeks_hedge import fetch_scored_candidates, fetch_spot, get_market_context

spot = fetch_spot('BTC')
ctx  = get_market_context('BTC')
print("Spot: {:,.0f}  DVOL: {:.1f}%  HV10d: {:.1f}%  IV/HV: {:.2f}x  Signal: {}".format(
    spot, ctx['curr_iv'], ctx['hv_10d'], ctx['iv_hv_ratio'],
    "OK" if ctx['signal_ok'] else "OFF"
))
print()

df = fetch_scored_candidates('BTC', spot, ctx['hv_10d'], ctx['iv_min'], ctx['iv_max'], ctx['curr_iv'], tte_min=1.0)
cols = ['instrument_name','tte_days','delta','gamma_pts','score_raw','score','yield_ann_pct','ba_pct']
print(df[cols].head(15).to_string(index=False))
