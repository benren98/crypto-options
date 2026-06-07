import csv, json, glob
from pathlib import Path

snapshots = sorted(glob.glob("output/pnl_snap_*.csv"))
if snapshots:
    with open(snapshots[-1]) as f:
        row = list(csv.DictReader(f))[0]
    summary = {
        "timestamp":           row.get("timestamp"),
        "days_held":           row.get("days_held"),
        "spot":                row.get("spot"),
        "entry_spot":          row.get("entry_spot"),
        "spot_move_pct":       row.get("spot_move_pct"),
        "entry_price_btc":     row.get("entry_price_btc"),
        "current_price_btc":   row.get("current_price_btc"),
        "current_ask_btc":     row.get("current_ask_btc"),
        "current_bid_btc":     row.get("current_bid_btc"),
        "entry_iv_pct":        row.get("entry_iv_pct"),
        "current_iv_pct":      row.get("current_iv_pct"),
        "pnl_option_usd":      row.get("pnl_option_usd"),
        "pnl_hedge_usd":       row.get("pnl_hedge_usd"),
        "total_pnl_usd":       row.get("total_pnl_usd"),
        "pnl_pct_of_premium":  row.get("pnl_pct_of_premium"),
        "tte_days":             row.get("tte_days"),
        "live_delta":           row.get("live_delta"),
        "live_vega":            row.get("live_vega"),
        "hedge_qty":            row.get("hedge_qty"),
        "hedge_delta_drift":    row.get("hedge_delta_drift"),
        "theta_daily_now_usd":  row.get("theta_daily_now_usd"),
        "theta_theory_usd":     row.get("theta_theory_usd"),
        "vrp_capture_pct":      row.get("vrp_capture_pct"),
    }
    Path("pnl_summary.json").write_text(json.dumps(summary, indent=2))
    print("pnl_summary.json cree depuis:", snapshots[-1])
else:
    print("Pas de snapshot trouve dans output/")
