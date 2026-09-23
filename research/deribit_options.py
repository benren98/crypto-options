"""
Deribit Options Market Data Fetcher
Fetches instruments, order books, greeks/mark prices, and recent trades.
All data exported as pandas DataFrames and saved to CSV.
"""

import time
import warnings
import requests
import pandas as pd
from datetime import datetime
from pathlib import Path
from urllib3.exceptions import InsecureRequestWarning

# SSL verification is disabled because the local network performs TLS inspection,
# which breaks certificate chain validation against public CAs.
warnings.filterwarnings("ignore", category=InsecureRequestWarning)

BASE_URL = "https://www.deribit.com/api/v2/public"
SSL_VERIFY = False
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)


def get(method: str, params: dict) -> dict:
    resp = requests.get(f"{BASE_URL}/{method}", params=params, timeout=10, verify=SSL_VERIFY)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Deribit error: {data['error']}")
    return data["result"]


# ── 1. Instruments ────────────────────────────────────────────────────────────

def fetch_instruments(currency: str = "BTC") -> pd.DataFrame:
    """List all active option instruments for a currency."""
    print(f"[instruments] Fetching {currency} options...")
    raw = get("get_instruments", {"currency": currency, "kind": "option", "expired": "false"})
    df = pd.DataFrame(raw)
    cols = [
        "instrument_name", "base_currency", "quote_currency",
        "strike", "option_type", "expiration_timestamp",
        "creation_timestamp", "min_trade_amount", "tick_size",
        "contract_size", "settlement_currency",
    ]
    cols = [c for c in cols if c in df.columns]
    df = df[cols].copy()
    df["expiration_dt"] = pd.to_datetime(df["expiration_timestamp"], unit="ms")
    df.sort_values(["expiration_dt", "strike"], inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


# ── 2. Tickers (mark price + greeks) ─────────────────────────────────────────

def fetch_ticker(instrument_name: str) -> dict | None:
    try:
        raw = get("ticker", {"instrument_name": instrument_name})
        greeks = raw.get("greeks", {})
        return {
            "instrument_name": instrument_name,
            "mark_price": raw.get("mark_price"),
            "mark_iv": raw.get("mark_iv"),
            "bid_price": raw.get("best_bid_price"),
            "ask_price": raw.get("best_ask_price"),
            "bid_iv": raw.get("bid_iv"),
            "ask_iv": raw.get("ask_iv"),
            "last_price": raw.get("last_price"),
            "open_interest": raw.get("open_interest"),
            "volume": raw.get("stats", {}).get("volume"),
            "delta": greeks.get("delta"),
            "gamma": greeks.get("gamma"),
            "vega": greeks.get("vega"),
            "theta": greeks.get("theta"),
            "rho": greeks.get("rho"),
            "underlying_price": raw.get("underlying_price"),
            "timestamp": raw.get("timestamp"),
        }
    except Exception as e:
        print(f"  [ticker] Error on {instrument_name}: {e}")
        return None


def fetch_all_tickers(instruments: list[str], batch_delay: float = 0.05) -> pd.DataFrame:
    print(f"[tickers] Fetching {len(instruments)} tickers...")
    rows = []
    for i, name in enumerate(instruments):
        row = fetch_ticker(name)
        if row:
            rows.append(row)
        if i % 50 == 49:
            print(f"  {i+1}/{len(instruments)} done")
            time.sleep(batch_delay)
    df = pd.DataFrame(rows)
    if not df.empty:
        df["snapshot_dt"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


# ── 3. Order book ─────────────────────────────────────────────────────────────

def fetch_order_book(instrument_name: str, depth: int = 10) -> dict[str, pd.DataFrame]:
    """Returns {'bids': df, 'asks': df} for a single instrument."""
    raw = get("get_order_book", {"instrument_name": instrument_name, "depth": depth})

    def side_df(entries: list, side: str) -> pd.DataFrame:
        df = pd.DataFrame(entries, columns=["price", "amount"])
        df.insert(0, "side", side)
        df.insert(0, "instrument_name", instrument_name)
        return df

    return {
        "bids": side_df(raw.get("bids", []), "bid"),
        "asks": side_df(raw.get("asks", []), "ask"),
        "mark_price": raw.get("mark_price"),
        "timestamp": raw.get("timestamp"),
    }


def fetch_order_books(instruments: list[str], depth: int = 10) -> pd.DataFrame:
    print(f"[order books] Fetching for {len(instruments)} instruments (depth={depth})...")
    frames = []
    for i, name in enumerate(instruments):
        try:
            ob = fetch_order_book(name, depth)
            frames.append(ob["bids"])
            frames.append(ob["asks"])
        except Exception as e:
            print(f"  [ob] Error on {name}: {e}")
        if i % 20 == 19:
            time.sleep(0.05)
    if frames:
        return pd.concat(frames, ignore_index=True)
    return pd.DataFrame()


# ── 4. Recent trades ──────────────────────────────────────────────────────────

def fetch_last_trades(instrument_name: str, count: int = 20) -> pd.DataFrame:
    raw = get("get_last_trades_by_instrument", {
        "instrument_name": instrument_name,
        "count": count,
        "sorting": "desc",
    })
    trades = raw.get("trades", [])
    if not trades:
        return pd.DataFrame()
    df = pd.DataFrame(trades)
    cols = ["trade_id", "instrument_name", "price", "amount",
            "direction", "iv", "timestamp", "tick_direction", "index_price"]
    cols = [c for c in cols if c in df.columns]
    df = df[cols].copy()
    df["trade_dt"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


def fetch_all_trades(instruments: list[str], count_per_instrument: int = 20) -> pd.DataFrame:
    print(f"[trades] Fetching recent trades for {len(instruments)} instruments...")
    frames = []
    for i, name in enumerate(instruments):
        try:
            df = fetch_last_trades(name, count_per_instrument)
            if not df.empty:
                frames.append(df)
        except Exception as e:
            print(f"  [trades] Error on {name}: {e}")
        if i % 20 == 19:
            time.sleep(0.05)
    if frames:
        return pd.concat(frames, ignore_index=True)
    return pd.DataFrame()


# ── Main ──────────────────────────────────────────────────────────────────────

def run(
    currencies: list[str] = None,
    max_instruments: int = 50,   # set None for all
    ob_depth: int = 10,
    trades_per_instrument: int = 20,
):
    if currencies is None:
        currencies = ["BTC", "ETH"]

    snapshot_tag = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

    for currency in currencies:
        print(f"\n{'='*50}")
        print(f" {currency} OPTIONS  —  {snapshot_tag}")
        print(f"{'='*50}")

        # 1. Instruments
        instruments_df = fetch_instruments(currency)
        if max_instruments:
            instruments_df = instruments_df.head(max_instruments)
        names = instruments_df["instrument_name"].tolist()
        print(f"  > {len(names)} instruments selected")

        # 2. Tickers + Greeks
        tickers_df = fetch_all_tickers(names)

        # 3. Order books (limit to first 20 to avoid rate limits in demo)
        ob_names = names[:20]
        ob_df = fetch_order_books(ob_names, depth=ob_depth)

        # 4. Recent trades
        trades_df = fetch_all_trades(names, count_per_instrument=trades_per_instrument)

        # ── Save to CSV ───────────────────────────────────────────────────────
        prefix = OUTPUT_DIR / f"{currency}_{snapshot_tag}"

        instruments_df.to_csv(f"{prefix}_instruments.csv", index=False)
        print(f"\n[saved] {prefix}_instruments.csv  ({len(instruments_df)} rows)")

        if not tickers_df.empty:
            tickers_df.to_csv(f"{prefix}_tickers.csv", index=False)
            print(f"[saved] {prefix}_tickers.csv  ({len(tickers_df)} rows)")

        if not ob_df.empty:
            ob_df.to_csv(f"{prefix}_orderbook.csv", index=False)
            print(f"[saved] {prefix}_orderbook.csv  ({len(ob_df)} rows)")

        if not trades_df.empty:
            trades_df.to_csv(f"{prefix}_trades.csv", index=False)
            print(f"[saved] {prefix}_trades.csv  ({len(trades_df)} rows)")

        # ── Quick summary ─────────────────────────────────────────────────────
        print(f"\n── {currency} Tickers summary ──")
        if not tickers_df.empty:
            summary_cols = ["instrument_name", "mark_price", "mark_iv", "delta", "open_interest"]
            summary_cols = [c for c in summary_cols if c in tickers_df.columns]
            print(tickers_df[summary_cols].head(10).to_string(index=False))

    print(f"\nDone. All files saved to: {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    run(
        currencies=["BTC", "ETH"],
        max_instruments=50,   # increase or set None for full universe
        ob_depth=10,
        trades_per_instrument=20,
    )
