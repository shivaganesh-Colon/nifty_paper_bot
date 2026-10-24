"""
Test the Supertrend OPTIONS strategy on past days - for local testing only.
Uses exactly the same Supertrend settings as the bot (supertrend.py + your config.py)
but never touches bot.py or places/records any paper trades.

    python simulate.py --date 2026-10-21
    python simulate.py --date 2026-10-07 --date 2026-10-21
    python simulate.py --from 2026-10-01 --to 2026-10-21        (one summary line per day)
    python simulate.py --date 2026-10-19 --set ST_STOP_POINTS=5 --set ST_TRAIL_START=5 --set ST_TRAIL_POINTS=5 --set ST_TRAIL_MODE=tick
                                                (try other settings for this test only - config.py is not changed)
    python daytest.py  --date 2026-10-21                         (same thing)

Data it reads (download with fetch_history.py):
    data/history/nifty_5min_history.csv     needed  - Supertrend signals
    data/history/nifty_minute_history.csv   optional - more accurate target / trailing-stop exits
    data/chain_DATE.csv                     optional - REAL option prices (days the bot recorded)
Without real option prices, option P&L is ESTIMATED:
    NIFTY points x 0.5 (ATM delta) - time decay (~7 premium pts per 3 hours) - Rs 125 costs.
"""
import argparse
import csv
import os
from collections import defaultdict
from datetime import date, timedelta

import config as C
import supertrend as STM

FILE_5M = "data/history/nifty_5min_history.csv"
FILE_1M = "data/history/nifty_minute_history.csv"
LOT_QTY = 65


def mins(t):
    return int(t[:2]) * 60 + int(t[3:5])


def hhmm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def load_candles(path):
    days = defaultdict(list)
    if not os.path.exists(path):
        return days
    with open(path) as f:
        for r in csv.DictReader(f):
            days[r["date"]].append({"time": r["time"], "open": float(r["open"]), "high": float(r["high"]),
                                    "low": float(r["low"]), "close": float(r["close"])})
    for d in days:
        days[d].sort(key=lambda c: c["time"])
    return days


def five_from_one(one):
    """Build 5-min candles from 1-min candles (anchored at 09:15)."""
    out = defaultdict(list)
    for d, cs in one.items():
        g = defaultdict(list)
        for c in cs:
            g[(mins(c["time"]) - 555) // 5].append(c)
        for k in sorted(g):
            x = g[k]
            out[d].append({"time": hhmm(555 + k * 5), "open": x[0]["open"], "high": max(c["high"] for c in x),
                           "low": min(c["low"] for c in x), "close": x[-1]["close"]})
    return out


class RealPrices:
    """Option last-traded prices recorded by the bot (data/chain_DATE.csv), if available."""
    def __init__(self, day):
        self.snaps = {}
        path = f"data/chain_{day}.csv"
        if os.path.exists(path):
            with open(path) as f:
                for r in csv.DictReader(f):
                    self.snaps.setdefault(r["time"][:5], {})[float(r["strike"])] = (float(r["ce_ltp"]), float(r["pe_ltp"]))
        self.times = sorted(self.snaps)

    def ok(self):
        return bool(self.times)

    def price(self, t, strike, typ):
        later = [x for x in self.times if x >= t]
        snap = self.snaps[later[0] if later else self.times[-1]]
        if strike not in snap:
            return None
        return snap[strike][0 if typ == "CE" else 1]


def estimate_rs(pts, held_min):
    decay = min(8, held_min / 180 * 7)
    return (pts * 0.5 - decay) * LOT_QTY * C.LOTS - 125


def run_day(day, c5_all, c1_all, verbose=True):
    days = sorted(c5_all)
    if day not in c5_all:
        print(f"{day}: no 5-min data for this date (holiday, weekend, or outside the history file).")
        return None
    i = days.index(day)
    d0 = date.fromisoformat(day)
    warm = [c for dd in days[:i] if date.fromisoformat(dd) >= d0 - timedelta(days=STM.ST_WARMUP_DAYS)
            for c in c5_all[dd]]
    today5 = c5_all[day]
    one = c1_all.get(day)
    path = one if one else today5          # price path for target / trail checks
    step = 1 if one else 5
    real = RealPrices(day)
    st_start, st_last, exit_t = mins(STM.ST_START), mins(STM.ST_LAST_ENTRY), mins(C.EXIT_TIME)

    # Supertrend direction at each 5-min close of the day (using warm-up history)
    dirs, lines = STM.compute(warm + today5)
    dirs, lines = dirs[len(warm):], lines[len(warm):]
    close_at = {mins(c["time"]) + 5: (k, c) for k, c in enumerate(today5)}

    trades, flips = [], []
    pos = None              # dict for the open trade
    last_dir, first_done = None, False

    def exit_trade(t_end, px, why):
        nonlocal pos
        s = 1 if pos["dir"] == "CE" else -1
        pts = s * (px - pos["entry"])
        held = t_end - pos["t"]
        rs, kind = None, "est"
        if real.ok():
            p_in = real.price(hhmm(pos["t"]), pos["strike"], pos["dir"])
            p_out = real.price(hhmm(t_end), pos["strike"], pos["dir"])
            if p_in and p_out:
                rs, kind = (p_out - p_in) * LOT_QTY * C.LOTS - 60, "real"
                pos["prem"] = (p_in, p_out)
        if rs is None:
            rs = estimate_rs(pts, held)
        trades.append(dict(entry_t=hhmm(pos["t"]), exit_t=hhmm(t_end), dir=pos["dir"], strike=pos["strike"],
                           entry=pos["entry"], exit=px, why=why, pts=pts, best=pos["best"], rs=rs, kind=kind,
                           prem=pos.get("prem")))
        pos = None

    for c in path:
        t0 = mins(c["time"])
        t1 = t0 + step
        # --- inside this bar: trailing stop first (pessimistic), then target, then best price ---
        if pos and STM.ST_TRAIL_MODE == "tick":
            s = 1 if pos["dir"] == "CE" else -1
            green = c["close"] >= c["open"]
            seq = ([c["open"], c["low"], c["high"], c["close"]] if green
                   else [c["open"], c["high"], c["low"], c["close"]])
            for px in seq:
                if pos["sl"] is not None and s * (px - pos["sl"]) <= 0:
                    fill = c["open"] if (px == c["open"] and s * (c["open"] - pos["sl"]) < 0) else pos["sl"]
                    locked = s * (pos["sl"] - pos["entry"])
                    exit_trade(t1, fill, f"trailing stop ({locked:+.0f} locked)" if locked > -STM.ST_STOP_POINTS
                               else f"stop-loss -{STM.ST_STOP_POINTS}")
                    break
                if STM.ST_TARGET_POINTS and s * (px - pos["entry"]) >= STM.ST_TARGET_POINTS:
                    exit_trade(t1, pos["entry"] + s * STM.ST_TARGET_POINTS, f"target +{STM.ST_TARGET_POINTS}")
                    break
                pos["best"] = max(pos["best"], s * (px - pos["entry"]))
                if STM.ST_TRAIL_POINTS and pos["best"] >= STM.ST_TRAIL_START:
                    n = pos["entry"] + s * (pos["best"] - STM.ST_TRAIL_POINTS)
                    if pos["sl"] is None or s * (n - pos["sl"]) > 0:
                        pos["sl"] = n
        elif pos:
            s = 1 if pos["dir"] == "CE" else -1
            adv, fav = (c["low"], c["high"]) if s == 1 else (c["high"], c["low"])
            if pos["sl"] is not None and s * (adv - pos["sl"]) <= 0:
                fill = c["open"] if s * (c["open"] - pos["sl"]) < 0 else pos["sl"]
                locked = s * (pos["sl"] - pos["entry"])
                exit_trade(t1, fill, f"trailing stop ({locked:+.0f} locked)" if locked > -STM.ST_STOP_POINTS
                           else f"stop-loss -{STM.ST_STOP_POINTS}")
            elif STM.ST_TARGET_POINTS and s * (fav - pos["entry"]) >= STM.ST_TARGET_POINTS:
                exit_trade(t1, pos["entry"] + s * STM.ST_TARGET_POINTS, f"target +{STM.ST_TARGET_POINTS}")
            else:
                pos["best"] = max(pos["best"], s * (fav - pos["entry"]))
        # --- a 5-min candle closes at t1 ---
        if t1 in close_at:
            k, c5 = close_at[t1]
            d_now, line = dirs[k], lines[k]
            if d_now is None:
                continue
            if last_dir is not None and d_now != last_dir:
                flips.append(f"{hhmm(t1)} {'GREEN' if d_now == 1 else 'RED'} @ {c5['close']:.0f}")
            fresh = last_dir is not None and d_now != last_dir
            first = STM.ST_FIRST_TRADE and not first_done and t1 >= st_start
            if t1 >= st_start:
                first_done = True
            if last_dir is None:
                flips.append(f"start {'GREEN' if d_now == 1 else 'RED'}")
            last_dir = d_now
            if pos:
                s = 1 if pos["dir"] == "CE" else -1
                if t1 >= exit_t:
                    exit_trade(t1, c5["close"], "day end")
                    continue
                if STM.ST_TRAIL_POINTS and pos["best"] >= STM.ST_TRAIL_START:
                    n = pos["entry"] + s * (pos["best"] - STM.ST_TRAIL_POINTS)
                    if pos["sl"] is None or s * (n - pos["sl"]) > 0:
                        pos["sl"] = n
                if d_now != s:
                    exit_trade(t1, c5["close"], "Supertrend flip")
            if pos is None and (fresh or first) and st_start <= t1 <= st_last:
                strike = round(c5["close"] / C.STRIKE_STEP) * C.STRIKE_STEP
                sgn = 1 if d_now == 1 else -1
                pos = dict(dir="CE" if d_now == 1 else "PE", entry=c5["close"], t=t1, strike=strike, best=0.0,
                           sl=c5["close"] - sgn * STM.ST_STOP_POINTS if STM.ST_STOP_POINTS else None)
        if t1 >= exit_t and pos:
            exit_trade(t1, c["close"], "day end")

    hi = max(c["high"] for c in today5)
    lo = min(c["low"] for c in today5)
    net = today5[-1]["close"] - today5[0]["open"]
    pathlen = sum(abs(c["close"] - c["open"]) for c in today5)
    eff = abs(net) / pathlen if pathlen else 0
    kind = f"range {hi - lo:.0f}, net {net:+.0f}"
    total = sum(t["rs"] for t in trades)
    if verbose:
        print("=" * 78)
        print(f" {day}  |  Supertrend({STM.ST_PERIOD},{STM.ST_MULT})  target {STM.ST_TARGET_POINTS}  "
              f"stop {STM.ST_STOP_POINTS}  trail {STM.ST_TRAIL_POINTS} after +{STM.ST_TRAIL_START} ({STM.ST_TRAIL_MODE})  |  exits on {'1-min' if one else '5-min'} data")
        print("=" * 78)
        print(f" NIFTY open {today5[0]['open']:,.0f}  high {hi:,.0f}  low {lo:,.0f}  close {today5[-1]['close']:,.0f}"
              f"  (range {hi - lo:.0f} pts, open-to-close {net:+.0f})")
        print(f" Supertrend: {' | '.join(flips)}")
        print(f" Option prices: {'REAL (recorded by the bot)' if real.ok() else 'ESTIMATED'}\n")
        if not trades:
            print("  No trades.")
        for t in trades:
            prem = f"  premium {t['prem'][0]:.1f} -> {t['prem'][1]:.1f}" if t["prem"] else ""
            print(f"  {t['entry_t']} BUY {t['strike']:.0f} {t['dir']}  NIFTY {t['entry']:,.1f} -> {t['exit']:,.1f} "
                  f"at {t['exit_t']} ({t['why']})")
            print(f"        best +{t['best']:.0f} | {t['pts']:+.1f} NIFTY pts{prem} | "
                  f"P&L Rs {t['rs']:+,.0f} ({t['kind']})")
        print(f"\n DAY TOTAL: {len(trades)} trades, {sum(t['pts'] for t in trades):+.1f} NIFTY pts, "
              f"Rs {total:+,.0f} ({C.LOTS} lot)\n")
    return dict(day=day, trades=len(trades), pts=sum(t["pts"] for t in trades), rs=total, kind=kind)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Test the Supertrend options strategy on past days")
    ap.add_argument("--date", action="append", default=[], help="YYYY-MM-DD (repeat for more days)")
    ap.add_argument("--from", dest="date_from")
    ap.add_argument("--to", dest="date_to")
    ap.add_argument("--set", action="append", default=[],
                    help="try a setting for this test only, e.g. --set ST_STOP_POINTS=5")
    a = ap.parse_args(argv)
    for kv in a.set:
        k, v = kv.split("=", 1)
        try:
            v = float(v) if "." in v else int(v)
        except ValueError:
            pass
        if not hasattr(STM, k) and not hasattr(C, k):
            raise SystemExit(f"Unknown setting {k}")
        setattr(STM if hasattr(STM, k) else C, k, v)
        print(f"  (test only) {k} = {v}")
    one = load_candles(FILE_1M)
    five = load_candles(FILE_5M)
    if not five and one:
        five = five_from_one(one)
    if not five:
        raise SystemExit(f"No data. Put {FILE_5M} (or {FILE_1M}) in place - run fetch_history.py.")
    if a.date:
        for d in a.date:
            run_day(d, five, one)
    elif a.date_from or a.date_to:
        lo, hi = a.date_from or "0000", a.date_to or "9999"
        rows = [r for r in (run_day(d, five, one, verbose=False) for d in sorted(five) if lo <= d <= hi) if r]
        print(f"{'date':<12}{'trades':>7}{'NIFTY pts':>11}{'P&L Rs':>11}   NIFTY that day")
        for r in rows:
            print(f"{r['day']:<12}{r['trades']:>7}{r['pts']:>+11.0f}{r['rs']:>+11,.0f}   {r['kind']}")
        if rows:
            tot = sum(r["rs"] for r in rows)
            print(f"\n{len(rows)} days, {sum(r['trades'] for r in rows)} trades, total Rs {tot:+,.0f}, "
                  f"winning days {sum(r['rs'] > 0 for r in rows)}/{len(rows)}")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
