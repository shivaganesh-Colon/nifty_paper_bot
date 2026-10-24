"""
Replay a recorded day through the bot's real rules - no Zerodha needed.
Uses the files the bot saves every day:
    data/chain_DATE.csv        (OI + option prices every minute)
    data/nifty_5min_DATE.csv   (NIFTY candles)

    python replay.py 2026-10-21
    python replay.py 2026-10-21 --prev 23175 22845 22889      (previous day high low close)
    python replay.py 2026-10-21 --set MIN_SCORE=2 --set MAX_RISK_POINTS=80

--set changes a setting for this replay only (config.py is not touched),
so you can test "what if" ideas on real recorded days.
Option prices come from the minute snapshots (last traded price), so fills
are approximate. Results go to replay_output/.
"""
import argparse
import csv
import logging
import os
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import config as C
import paper
import trend
from bot import Day, evaluate, trade_args
from levels import ChainTracker
from market import IST, hhmm
from paper import PaperBook

log = logging.getLogger("bot")


class RecordedTracker(ChainTracker):
    """ChainTracker fed from the saved CSV instead of live quotes."""
    def __init__(self):
        super().__init__(market=None, chain={}, record_file=os.devnull)

    def push(self, when, spot, snap):
        self.history.append((when, spot, snap))


def load(day):
    snaps = defaultdict(dict)
    spots = {}
    with open(f"data/chain_{day}.csv") as f:
        for r in csv.DictReader(f):
            t = datetime.combine(datetime.fromisoformat(day).date(),
                                 datetime.strptime(r["time"], "%H:%M:%S").time(), IST)
            spots[t] = float(r["spot"])
            snaps[t][float(r["strike"])] = {"CE_oi": float(r["ce_oi"]), "PE_oi": float(r["pe_oi"]),
                                            "CE_ltp": float(r["ce_ltp"]), "PE_ltp": float(r["pe_ltp"])}
    candles = []
    with open(f"data/nifty_{C.CANDLE_MINUTES}min_{day}.csv") as f:
        for r in csv.DictReader(f):
            t = datetime.combine(datetime.fromisoformat(day).date(),
                                 datetime.strptime(r["time"], "%H:%M").time(), IST)
            candles.append({"time": t, **{k: float(r[k]) for k in ("open", "high", "low", "close")}})
    return [(t, spots[t], snaps[t]) for t in sorted(snaps)], candles


def apply_overrides(pairs):
    for p in pairs:
        name, val = p.split("=", 1)
        try:
            val = eval(val, {}, {})
        except Exception:
            pass
        setattr(C, name, val)
        for mod in (paper, trend):
            if hasattr(mod, name):
                setattr(mod, name, val)
        print(f"  override: {name} = {val!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("day")
    ap.add_argument("--prev", nargs=3, type=float, metavar=("HIGH", "LOW", "CLOSE"))
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--lot-size", type=int, default=65)
    a = ap.parse_args()
    apply_overrides(a.set)

    out = "replay_output"
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    log.setLevel(logging.INFO)
    log.handlers.clear()
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(h)

    snaps, all_candles = load(a.day)
    prev = {"high": a.prev[0], "low": a.prev[1], "close": a.prev[2]} if a.prev else None
    d0 = datetime.fromisoformat(a.day).date()
    at = lambda s: datetime.combine(d0, hhmm(s), IST)
    start_t, last_entry_t, exit_t = at(C.NO_TRADE_BEFORE), at(C.NO_NEW_TRADE_AFTER), at(C.EXIT_TIME)
    step = timedelta(minutes=C.CANDLE_MINUTES)

    tracker = RecordedTracker()
    book = PaperBook(f"{out}/trades.csv", log)
    day = Day()
    trade, trade_key, last_candle = None, None, None

    if prev and all_candles:
        gap = abs(all_candles[0]["open"] - prev["close"]) / prev["close"] * 100
        print(f"Gap at open: {gap:.2f}%")
        if gap > C.GAP_SKIP_PCT:
            day.blocked = f"gap {gap:.1f}%"

    for now, spot, snap in snaps:
        tracker.push(now, spot, snap)
        if trade:
            strike, typ = trade_key
            ltp = snap.get(strike, {}).get(f"{typ}_ltp", 0)
            done = book.manage(trade, now, spot, ltp, ltp)
            if not done and now >= exit_t:
                book.close(trade, now, spot, ltp, ltp, "time exit")
                done = True
            if done:
                net = book.record(trade)
                day.pnl += net
                day.losses += net <= 0
                day.check()
                trade = None
        if now >= exit_t:
            break
        candles = [c for c in all_candles if c["time"] + step <= now]
        if candles and candles[-1]["time"] != last_candle:
            last_candle = candles[-1]["time"]
            res, sup = tracker.levels(spot)
            if trade is None and day.blocked is None and start_t <= now <= last_entry_t:
                sig = evaluate(now, candles, spot, tracker, prev, day, f"{out}/signals.csv")
                if sig:
                    atm = round(spot / C.STRIKE_STEP) * C.STRIKE_STEP
                    ltp = snap.get(float(atm), {}).get(f"{sig['direction']}_ltp", 0)
                    if ltp > 0:
                        day.register(sig)
                        trade_key = (float(atm), sig["direction"])
                        trade = book.open(**trade_args(
                            sig, f"NIFTY {atm:.0f} {sig['direction']}", a.lot_size, now, spot,
                            sig["risk"], paper.buy_price(ltp, ltp)))
            print(f"{now:%H:%M}  NIFTY {spot:9.1f}  R {[int(x) for x in res]}  S {[int(x) for x in sup]}")

    print(f"\nReplay finished. Trades: {day.trades}  P&L: Rs {day.pnl:.0f}")
    if not os.path.exists(f"{out}/signals.csv"):
        print("No setup formed at any OI level.")


if __name__ == "__main__":
    main()
