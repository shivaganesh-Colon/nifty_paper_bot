"""
Download past NIFTY 50 5-minute candles for backtesting.
Run after  python login.py  (uses today's login):

    python fetch_history.py               # last 365 days, 5-minute candles
    python fetch_history.py 180           # last 180 days
    python fetch_history.py 365 minute    # last 365 days, 1-minute candles

Saves: data/history/nifty_5min_history.csv  or  nifty_minute_history.csv
Send that file to Claude for the backtest.
"""
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta

from kiteconnect import KiteConnect

import config as C
from market import IST


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 365
    interval = sys.argv[2] if len(sys.argv) > 2 else "5minute"
    if interval not in ("5minute", "minute"):
        sys.exit("Interval must be 5minute or minute")
    with open("access_token.json") as f:
        tok = json.load(f)
    if tok["date"] != datetime.now(IST).date().isoformat():
        sys.exit("Login is from an earlier day. Run:  python login.py")
    kite = KiteConnect(api_key=C.API_KEY)
    kite.set_access_token(tok["access_token"])

    end = datetime.now(IST)
    start = end - timedelta(days=days)
    # Zerodha limits per request: 100 days for 5-minute, 60 days for 1-minute candles
    rows, chunk = [], timedelta(days=90 if interval == "5minute" else 55)
    t = start
    while t < end:
        t2 = min(t + chunk, end)
        print(f"Downloading {t:%d %b %Y} to {t2:%d %b %Y} ...")
        for attempt in range(3):
            try:
                data = kite.historical_data(C.SPOT_TOKEN, t, t2, interval)
                break
            except Exception as e:
                print(f"  retrying ({e})")
                time.sleep(2)
        else:
            sys.exit("Download failed - check your internet and try again.")
        for c in data:
            d = c["date"]
            rows.append((d.strftime("%Y-%m-%d"), d.strftime("%H:%M"),
                         c["open"], c["high"], c["low"], c["close"]))
        t = t2 + timedelta(seconds=1)
        time.sleep(0.5)

    rows = sorted(set(rows))
    os.makedirs("data/history", exist_ok=True)
    path = ("data/history/nifty_5min_history.csv" if interval == "5minute"
            else "data/history/nifty_minute_history.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "time", "open", "high", "low", "close"])
        w.writerows(rows)
    n_days = len({r[0] for r in rows})
    print(f"\nSaved {len(rows)} candles ({n_days} trading days) to {path}")
    print("Send this file to Claude.")


if __name__ == "__main__":
    main()
