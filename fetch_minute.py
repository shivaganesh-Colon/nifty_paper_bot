"""
Downloads NIFTY 50 index 1-minute candles from Kite (historical data) for the last 3 years
and saves them in the same format Claude already uses for the 1-year tests.

    python login.py          (if not already logged in today)
    python fetch_minute.py   (about 1-2 minutes)

Output: data/nifty_1min_3y.csv   (date,time,open,high,low,close)
Zip it and send it to Claude.

Kite gives at most 60 days of 1-minute data per request, so the script asks in 60-day pieces.
It can run during market hours - it only reads data, it never places orders.
"""
import csv
import json
import os
import sys
import time
from datetime import date, timedelta

from kiteconnect import KiteConnect

import config as C

YEARS = 3
SPOT_TOKEN = 256265          # NIFTY 50 index
CHUNK_DAYS = 60
OUT = os.path.join("data", "nifty_1min_3y.csv")


def kite_login():
    with open("access_token.json") as f:
        tok = json.load(f)
    if tok.get("date") != date.today().isoformat():
        sys.exit("access_token.json is not from today - run  python login.py  first.")
    k = KiteConnect(api_key=C.API_KEY)
    k.set_access_token(tok["access_token"])
    return k


def main():
    kite = kite_login()
    os.makedirs("data", exist_ok=True)
    end = date.today() - timedelta(days=1)            # up to yesterday (full days only)
    start = end - timedelta(days=365 * YEARS)
    rows = {}
    a = start
    n = 0
    while a <= end:
        b = min(a + timedelta(days=CHUNK_DAYS - 1), end)
        for attempt in range(3):
            try:
                data = kite.historical_data(SPOT_TOKEN, a, b, "minute")
                break
            except Exception as e:
                if attempt == 2:
                    print(f"  could not get {a} to {b}: {e}")
                    data = []
                time.sleep(2)
        for r in data:
            d = r["date"]
            t = d.strftime("%H:%M")
            if "09:15" <= t <= "15:29":
                rows[(d.strftime("%Y-%m-%d"), t)] = (r["open"], r["high"], r["low"], r["close"])
        n += 1
        print(f"  {a} to {b}: {len(data)} candles")
        a = b + timedelta(days=1)
        time.sleep(0.4)                                  # Kite limit: 3 requests / second
    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "time", "open", "high", "low", "close"])
        for (d, t), v in sorted(rows.items()):
            w.writerow([d, t] + list(v))
    days = len({d for d, _ in rows})
    print(f"\nSaved {OUT}: {len(rows):,} candles, {days} trading days "
          f"({min(rows)[0] if rows else '-'} to {max(rows)[0] if rows else '-'}).")
    print("Zip it (right-click -> Send to -> Compressed folder) and send it to Claude.")


if __name__ == "__main__":
    main()
