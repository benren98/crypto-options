"""Analyse de la distribution des yields annualisés sur tous les puts OTM scannables."""
import sys; sys.path.insert(0, '.')
from greeks_hedge import get, CURRENCY, SCAN_TTE_MIN, SCAN_TTE_MAX
from datetime import datetime, timezone

book = get("get_book_summary_by_currency", {"currency": "BTC", "kind": "option"})
spot = next(b["underlying_price"] for b in book if b.get("underlying_price"))

now = datetime.now(timezone.utc)
rows = []
for b in book:
    name = b["instrument_name"]
    parts = name.split("-")
    if len(parts) != 4 or parts[3] != "P":
        continue
    try:
        exp = datetime.strptime(parts[1], "%d%b%y").replace(hour=8, tzinfo=timezone.utc)
        strike = float(parts[2])
    except ValueError:
        continue
    tte_d = (exp - now).total_seconds() / 86400
    if not (SCAN_TTE_MIN <= tte_d <= SCAN_TTE_MAX):
        continue
    if strike >= spot:          # OTM puts seulement
        continue
    bid = b.get("bid_price") or 0
    if bid <= 0:
        continue
    otm_pct = (spot - strike) / spot * 100
    if otm_pct < 3 or otm_pct > 25:   # zone de chasse habituelle
        continue
    y_ann = bid / (tte_d / 365)
    rows.append((name, tte_d, otm_pct, bid, y_ann))

rows.sort(key=lambda r: -r[4])
print(f"Spot: {spot:,.0f} | {len(rows)} puts OTM (3-25% OTM, {SCAN_TTE_MIN}-{SCAN_TTE_MAX}j)")
print(f"{'Instrument':<26} {'TTE j':>6} {'OTM %':>6} {'bid':>8} {'yield ann':>10} {'s_yield@20%':>11}")
for name, tte, otm, bid, y in rows[:25]:
    print(f"{name:<26} {tte:>6.1f} {otm:>5.1f}% {bid:>8.4f} {y*100:>9.1f}% {min(1.0, y/0.20):>11.3f}")

ys = [r[4] for r in rows]
ys_sorted = sorted(ys)
import statistics
print()
print(f"Min     : {min(ys)*100:.1f}%")
print(f"P25     : {ys_sorted[len(ys)//4]*100:.1f}%")
print(f"Mediane : {statistics.median(ys)*100:.1f}%")
print(f"P75     : {ys_sorted[3*len(ys)//4]*100:.1f}%")
print(f"P90     : {ys_sorted[int(0.9*len(ys))]*100:.1f}%")
print(f"Max     : {max(ys)*100:.1f}%")
