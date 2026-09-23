"""
Volatility Surface & VRP Signal
================================
1. Fetches historical OHLCV data from Deribit (index price) to compute
   Realized Volatility (RV) over multiple lookback windows.
2. Pulls current Implied Volatility (IV) from the options surface for
   BTC and ETH (all active expirations).
3. Computes the VRP = IV - RV and flags entry signals for short-put strategies.
4. Plots the vol surface (strike vs expiry) and the IV/RV term structure.
"""

import warnings
import time
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib3.exceptions import InsecureRequestWarning

warnings.filterwarnings("ignore", category=InsecureRequestWarning)

BASE_URL  = "https://www.deribit.com/api/v2/public"
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def get(method: str, params: dict) -> dict:
    r = requests.get(f"{BASE_URL}/{method}", params=params,
                     timeout=15, verify=False)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"API error: {data['error']}")
    return data["result"]


def ts_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


# ── 1. Realized Volatility from historical index prices ───────────────────────

def fetch_index_history(currency: str = "BTC",
                        days: int = 90,
                        resolution: int = 3600) -> pd.DataFrame:
    """
    Fetches hourly index prices for the last `days` days.
    resolution in seconds: 3600 = 1h, 86400 = 1d
    """
    end_ms   = ts_now_ms()
    start_ms = end_ms - days * 86_400_000
    print(f"  [RV] Fetching {currency} index history ({days}d, res={resolution}s)...")

    raw = get("get_index_price_names", {})
    # index name: btc_usd or eth_usd
    index_name = f"{currency.lower()}_usd"

    # Deribit returns candles via get_tradingview_chart_data
    raw = get("get_tradingview_chart_data", {
        "instrument_name": f"{currency}-PERPETUAL",
        "start_timestamp": start_ms,
        "end_timestamp":   end_ms,
        "resolution":      str(resolution // 60),  # in minutes
    })
    df = pd.DataFrame({
        "ts":    raw["ticks"],
        "open":  raw["open"],
        "high":  raw["high"],
        "low":   raw["low"],
        "close": raw["close"],
        "vol":   raw["volume"],
    })
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("dt", inplace=True)
    df.sort_index(inplace=True)
    return df


def compute_realized_vol(price_df: pd.DataFrame,
                         windows: list[int] = [7, 14, 30, 60]) -> pd.DataFrame:
    """
    Computes annualized realized volatility (close-to-close log returns)
    for multiple rolling windows (in days).
    price_df must have hourly close prices.
    Returns a DataFrame indexed by date with one column per window.
    """
    closes  = price_df["close"].resample("1D").last().dropna()
    log_ret = np.log(closes / closes.shift(1)).dropna()

    rv_data = {}
    for w in windows:
        rv_data[f"RV_{w}d"] = log_ret.rolling(w).std() * np.sqrt(252)

    return pd.DataFrame(rv_data, index=log_ret.index)


# ── 2. Implied Volatility surface from live options ───────────────────────────

def fetch_iv_surface(currency: str = "BTC") -> pd.DataFrame:
    """
    Fetches all active option instruments and their mark IVs.
    Returns a DataFrame with columns: expiry_dt, strike, option_type, mark_iv, delta, tte_days
    """
    print(f"  [IV] Fetching {currency} IV surface...")
    instruments = get("get_instruments", {
        "currency": currency,
        "kind": "option",
        "expired": "false",
    })

    now_ms = ts_now_ms()
    rows   = []
    names  = [i["instrument_name"] for i in instruments]

    # Batch fetch tickers in groups to avoid rate limits
    for i, name in enumerate(names):
        try:
            t = get("ticker", {"instrument_name": name})
            exp_ms = next(
                x["expiration_timestamp"]
                for x in instruments if x["instrument_name"] == name
            )
            strike = next(
                x["strike"]
                for x in instruments if x["instrument_name"] == name
            )
            opt_type = "call" if name.endswith("-C") else "put"
            tte_days = (exp_ms - now_ms) / 86_400_000

            rows.append({
                "instrument_name": name,
                "expiry_ms":       exp_ms,
                "expiry_dt":       pd.to_datetime(exp_ms, unit="ms", utc=True),
                "tte_days":        round(tte_days, 2),
                "strike":          strike,
                "option_type":     opt_type,
                "mark_iv":         t.get("mark_iv"),
                "bid_iv":          t.get("bid_iv"),
                "ask_iv":          t.get("ask_iv"),
                "delta":           (t.get("greeks") or {}).get("delta"),
                "vega":            (t.get("greeks") or {}).get("vega"),
                "open_interest":   t.get("open_interest"),
                "mark_price":      t.get("mark_price"),
                "underlying_price":t.get("underlying_price"),
            })
        except Exception as e:
            print(f"    skip {name}: {e}")

        if i % 50 == 49:
            print(f"    {i+1}/{len(names)} tickers fetched")
            time.sleep(0.1)

    df = pd.DataFrame(rows).dropna(subset=["mark_iv"])
    df["mark_iv_pct"] = df["mark_iv"]       # already in % on Deribit
    return df


# ── 3. VRP signal ─────────────────────────────────────────────────────────────

def compute_vrp_signal(iv_df: pd.DataFrame, rv_df: pd.DataFrame,
                       rv_window: int = 30) -> pd.DataFrame:
    """
    Computes VRP = IV(ATM, per expiry bucket) - RV(rv_window days).
    Returns a term-structure DataFrame.
    """
    latest_rv = rv_df[f"RV_{rv_window}d"].dropna().iloc[-1] * 100  # in %

    # ATM proxy: |delta| closest to 0.5
    iv_df["abs_delta"] = iv_df["delta"].abs()
    atm = (iv_df.groupby(["expiry_dt", "option_type"])
               .apply(lambda g: g.nsmallest(2, "abs_delta"))
               .reset_index(drop=True))
    atm_iv = atm.groupby("expiry_dt")["mark_iv"].mean().reset_index()
    atm_iv.rename(columns={"mark_iv": "atm_iv"}, inplace=True)

    tte = iv_df.groupby("expiry_dt")["tte_days"].first().reset_index()
    ts  = atm_iv.merge(tte, on="expiry_dt")
    ts  = ts[ts["tte_days"] > 0].sort_values("tte_days")

    ts["rv_30d"]   = latest_rv
    ts["vrp"]      = ts["atm_iv"] - ts["rv_30d"]
    ts["vrp_pct"]  = ts["vrp"] / ts["atm_iv"] * 100  # VRP as % of IV
    ts["signal"]   = ts["vrp"] > 5  # entry signal: VRP > 5 vol points

    return ts, latest_rv


# ── 4. Short-put candidates (delta ~0.20) ─────────────────────────────────────

def find_short_put_candidates(iv_df: pd.DataFrame,
                               delta_target: float = -0.20,
                               delta_tol:    float =  0.05,
                               max_tte:      int   = 14) -> pd.DataFrame:
    """
    Filters OTM put candidates matching:
      - put option
      - tte <= max_tte days
      - delta in [delta_target - tol, delta_target + tol]
    """
    puts = iv_df[
        (iv_df["option_type"] == "put") &
        (iv_df["tte_days"] <= max_tte) &
        (iv_df["tte_days"] > 0.5) &
        (iv_df["delta"].between(delta_target - delta_tol,
                                delta_target + delta_tol))
    ].copy()

    puts["delta_dist"] = (puts["delta"] - delta_target).abs()
    puts = puts.sort_values(["tte_days", "delta_dist"])

    display_cols = ["instrument_name", "tte_days", "strike", "delta",
                    "mark_iv", "mark_price", "underlying_price", "open_interest"]
    return puts[display_cols].reset_index(drop=True)


# ── 5. Plots ──────────────────────────────────────────────────────────────────

def plot_rv_history(rv_df: pd.DataFrame, currency: str, save: bool = True):
    fig, ax = plt.subplots(figsize=(12, 5))
    colors = ["#2196F3", "#FF9800", "#4CAF50", "#9C27B0"]
    for col, c in zip(rv_df.columns, colors):
        ax.plot(rv_df.index, rv_df[col] * 100, label=col, color=c, linewidth=1.5)
    ax.set_title(f"{currency} Realized Volatility — multiple windows", fontsize=13)
    ax.set_ylabel("Annualized Vol (%)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    if save:
        path = OUTPUT_DIR / f"{currency}_realized_vol.png"
        fig.savefig(path, dpi=150)
        print(f"  [plot] {path}")
    plt.close(fig)


def plot_vrp_term_structure(ts: pd.DataFrame, rv: float,
                             currency: str, save: bool = True):
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    # Left: ATM IV vs RV by expiry
    ax = axes[0]
    ax.bar(ts["tte_days"], ts["vrp"], color=["#4CAF50" if v > 0 else "#F44336"
                                              for v in ts["vrp"]], alpha=0.7)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_title(f"{currency} VRP by Expiry (ATM IV - RV 30d = {rv:.1f}%)")
    ax.set_xlabel("Days to Expiry")
    ax.set_ylabel("VRP (vol points)")
    ax.grid(alpha=0.3)

    for _, row in ts.iterrows():
        ax.annotate(f"{row['tte_days']:.0f}d",
                    xy=(row["tte_days"], row["vrp"]),
                     ha="center", va="bottom", fontsize=7)

    # Right: ATM IV term structure
    ax2 = axes[1]
    ax2.plot(ts["tte_days"], ts["atm_iv"], "o-", color="#2196F3",
             label="ATM IV", linewidth=2)
    ax2.axhline(rv, color="#FF5722", linestyle="--", linewidth=1.5,
                label=f"RV 30d = {rv:.1f}%")
    ax2.fill_between(ts["tte_days"], rv, ts["atm_iv"],
                     where=(ts["atm_iv"] > rv), alpha=0.15, color="#4CAF50",
                     label="VRP > 0")
    ax2.set_title(f"{currency} IV Term Structure vs Realized Vol")
    ax2.set_xlabel("Days to Expiry")
    ax2.set_ylabel("Volatility (%)")
    ax2.legend()
    ax2.grid(alpha=0.3)

    fig.tight_layout()
    if save:
        path = OUTPUT_DIR / f"{currency}_vrp_term_structure.png"
        fig.savefig(path, dpi=150)
        print(f"  [plot] {path}")
    plt.close(fig)


def plot_iv_surface(iv_df: pd.DataFrame, currency: str,
                    underlying: float, save: bool = True):
    """Heatmap: strike (normalized as moneyness) vs expiry vs IV."""
    puts  = iv_df[iv_df["option_type"] == "put"].copy()
    calls = iv_df[iv_df["option_type"] == "call"].copy()

    fig, axes = plt.subplots(1, 2, figsize=(16, 6))

    for ax, df, title in zip(axes,
                              [puts, calls],
                              ["Puts IV Surface", "Calls IV Surface"]):
        if df.empty:
            continue
        df = df.copy()
        df["moneyness"] = df["strike"] / underlying

        pivot = df.pivot_table(values="mark_iv", index="strike",
                                columns="tte_days", aggfunc="mean")
        pivot = pivot.sort_index(ascending=False)

        im = ax.imshow(pivot.values,
                       aspect="auto", cmap="RdYlGn_r",
                       vmin=pivot.values[~np.isnan(pivot.values)].min(),
                       vmax=pivot.values[~np.isnan(pivot.values)].max())
        ax.set_xticks(range(len(pivot.columns)))
        ax.set_xticklabels([f"{c:.0f}d" for c in pivot.columns],
                            rotation=45, fontsize=7)
        ax.set_yticks(range(len(pivot.index)))
        ax.set_yticklabels([f"{int(k):,}" for k in pivot.index], fontsize=7)
        ax.set_xlabel("Days to Expiry")
        ax.set_ylabel("Strike")
        ax.set_title(f"{currency} {title}")
        plt.colorbar(im, ax=ax, label="IV (%)")

    fig.tight_layout()
    if save:
        path = OUTPUT_DIR / f"{currency}_iv_surface.png"
        fig.savefig(path, dpi=150)
        print(f"  [plot] {path}")
    plt.close(fig)


# ── Main ──────────────────────────────────────────────────────────────────────

def run(currencies: list[str] = None, rv_days: int = 90):
    if currencies is None:
        currencies = ["BTC", "ETH"]

    tag = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

    for currency in currencies:
        print(f"\n{'='*55}")
        print(f"  {currency}  —  Vol Surface & VRP Analysis  — {tag}")
        print(f"{'='*55}")

        # 1. Realized Vol
        print("\n[1/4] Computing Realized Volatility...")
        price_df = fetch_index_history(currency, days=rv_days, resolution=3600)
        rv_df    = compute_realized_vol(price_df, windows=[7, 14, 30, 60])
        latest   = rv_df.iloc[-1] * 100
        print(f"  Latest RV — 7d: {latest['RV_7d']:.1f}%  "
              f"14d: {latest['RV_14d']:.1f}%  "
              f"30d: {latest['RV_30d']:.1f}%  "
              f"60d: {latest['RV_60d']:.1f}%")

        # 2. IV Surface
        print("\n[2/4] Fetching Implied Volatility surface...")
        iv_df = fetch_iv_surface(currency)
        underlying = iv_df["underlying_price"].dropna().iloc[0]
        print(f"  {len(iv_df)} options loaded | Spot: ${underlying:,.0f}")

        # 3. VRP Signal
        print("\n[3/4] Computing VRP signal...")
        ts, rv_30d = compute_vrp_signal(iv_df, rv_df, rv_window=30)
        print("\n  VRP Term Structure:")
        print(f"  {'Expiry':<12} {'TTE':>6} {'ATM IV':>8} {'RV 30d':>8} {'VRP':>8} {'Signal':>8}")
        print("  " + "-"*56)
        for _, row in ts.iterrows():
            signal = "  SELL" if row["signal"] else "  -"
            print(f"  {str(row['expiry_dt'].date()):<12} "
                  f"{row['tte_days']:>6.1f} "
                  f"{row['atm_iv']:>8.1f}% "
                  f"{row['rv_30d']:>8.1f}% "
                  f"{row['vrp']:>+8.1f}  "
                  f"{signal}")

        # 4. Short-put candidates
        print("\n[4/4] Short-put candidates (delta ~-0.20, TTE <= 14d)...")
        candidates = find_short_put_candidates(iv_df)
        if candidates.empty:
            print("  No candidates found in current data.")
        else:
            print(candidates.head(10).to_string(index=False))

        # Save data
        iv_df.to_csv(OUTPUT_DIR / f"{currency}_{tag}_iv_surface.csv", index=False)
        ts.to_csv(OUTPUT_DIR / f"{currency}_{tag}_vrp_term_structure.csv", index=False)
        candidates.to_csv(OUTPUT_DIR / f"{currency}_{tag}_short_put_candidates.csv", index=False)

        # Plots
        print("\n[plots] Generating charts...")
        plot_rv_history(rv_df, currency)
        plot_vrp_term_structure(ts, rv_30d, currency)
        plot_iv_surface(iv_df, currency, underlying)

    print(f"\nDone. Outputs in: {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    run(currencies=["BTC", "ETH"], rv_days=90)
