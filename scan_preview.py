"""
scan_preview.py — Montre ce que le scorer SÉLECTIONNE réellement, sous le vrai skew
fité, avec la config actuelle (0.20/0.15/0.65, SKEW_NORM=0.60) vs l'ancienne
(0.40/0.30/0.30, SKEW_NORM=0.20). But : vérifier qu'on entre dans des positions
équilibrées et pas du deep-OTM à faible yield.
"""
import sys, math
sys.path.insert(0, '.')
import numpy as np
import backtest as bt
import vol_surface_data as vs

# Jour récent : spot/dvol + HV blend
days = bt.fetch_history(0.3)
closes = [d['spot'] for d in days]
spot, dvol = days[-1]['spot'], days[-1]['dvol']
hv10, hv30 = bt.hv_from(closes, 10), bt.hv_from(closes, 30)
hv_blend = 0.5*hv10 + 0.5*hv30

# Fit skew réel
otm_f, ratio_f = [], []
for _, _, m, iv, atm in vs.all_points():
    if m and iv and atm and m < 1:
        otm_f.append((1-m)*100); ratio_f.append(iv/atm)
otm_f = np.array(otm_f); ratio_f = np.array(ratio_f)
(a, b), *_ = np.linalg.lstsq(np.column_stack([otm_f, otm_f**2]), ratio_f-1, rcond=None)
def skew_fac(o): o = max(o, 0); return 1 + a*o + b*o*o

print(f"  Jour : spot {spot:,.0f}  DVOL {dvol:.1f}%  HV_blend {hv_blend:.1f}%  | skew fité 1+{a:.4f}·OTM+{b:.5f}·OTM²\n")

HC, TTE = 1.5, 14
T = TTE/365
rows = []
for mny in [0.97,0.95,0.93,0.91,0.89,0.87,0.85,0.83,0.81,0.79,0.77,0.75]:
    K = mny*spot
    otm = (1-mny)*100
    bid_iv = dvol*skew_fac(otm) - HC
    price, delta, gamma = bt.bs_put(spot, K, T, bid_iv/100)
    if price < bt.MIN_PREMIUM_USD:
        continue
    yld = (price/spot)/T
    z = (otm/100)/max(hv_blend/100*math.sqrt(T), 1e-9)
    skew = bid_iv/dvol - 1
    s_ivhv = max(0, min(1, bid_iv/hv_blend - 1))
    s_yld  = min(1, yld*z/0.30)
    g_pts  = gamma*spot*0.01*100
    gfac   = max(0, 1 - max(0, g_pts-5)/(10-5))
    s_skew_new = max(0, min(1, skew/0.60))
    s_skew_old = max(0, min(1, skew/0.20))
    sc_new = (0.20*s_ivhv + 0.15*s_yld + 0.65*s_skew_new)*gfac
    sc_old = (0.40*s_ivhv + 0.30*s_yld + 0.30*s_skew_old)*gfac
    rows.append(dict(otm=otm, K=K, delta=delta, prem=price, yld=yld*100,
                     s_ivhv=s_ivhv, s_yld=s_yld, skew=skew*100,
                     s_skew_new=s_skew_new, sc_new=sc_new, sc_old=sc_old))

print(f"  TTE {TTE}j — candidats (prime ≥ {bt.MIN_PREMIUM_USD:.0f}$) :")
print(f"  {'OTM%':>5} {'delta':>6} {'prime$':>7} {'yld%/an':>8} {'skew%':>6} {'s_ivhv':>6} {'s_yld':>6} {'s_skew':>6} {'SCORE_new':>9} {'(old)':>6}")
for r in rows:
    mark = ""
    print(f"  {r['otm']:>4.0f}% {r['delta']:>6.3f} {r['prem']:>6.0f}$ {r['yld']:>7.0f}% {r['skew']:>5.0f}% "
          f"{r['s_ivhv']:>6.2f} {r['s_yld']:>6.2f} {r['s_skew_new']:>6.2f} {r['sc_new']:>9.3f} {r['sc_old']:>6.3f}")

win_new = max(rows, key=lambda r: r['sc_new'])
win_old = max(rows, key=lambda r: r['sc_old'])
print(f"\n  >> NOUVELLE config choisit : OTM {win_new['otm']:.0f}%  delta {win_new['delta']:.3f}  "
      f"prime {win_new['prem']:.0f}$  yield {win_new['yld']:.0f}%/an  score {win_new['sc_new']:.3f}")
print(f"  >> ANCIENNE config aurait : OTM {win_old['otm']:.0f}%  delta {win_old['delta']:.3f}  "
      f"prime {win_old['prem']:.0f}$  yield {win_old['yld']:.0f}%/an")
print(f"\n  Seuil 0.50 — candidats au-dessus : {sum(1 for r in rows if r['sc_new']>=0.50)}/{len(rows)}")
