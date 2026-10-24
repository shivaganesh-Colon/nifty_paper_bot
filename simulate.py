"""
Practice run on a MADE-UP market day - no Zerodha account or internet needed.
Lets you see exactly how the bot thinks before Monday.

    python simulate.py           # V-shape reversal day
    python simulate.py trend     # trend day (reversal fails -> stop-loss)
    python simulate.py --date 2026-10-21              # Supertrend options strategy on a REAL past day
    python simulate.py --from 2026-10-01 --to 2026-10-21   # one line per day (see daytest.py)

Output goes to sim_output/ (kept separate from real paper-trading logs).
Prices, OI and option premiums here are synthetic, NOT real market data.
"""
import math
import random
import shutil
import sys
from datetime import datetime, timedelta, date

import alerts
import bot
from market import IST

DAY = date(2026, 10, 19)
EXPIRY = date(2026, 10, 27)

SCENARIOS = {
    # (minute after 9:15, NIFTY)
    "v": [(0, 25000), (45, 25040), (85, 25190), (90, 25212), (95, 25222), (100, 25185),
          (110, 25160), (140, 25120), (180, 25090), (240, 25070), (300, 25080), (375, 25060)],
    "trend": [(0, 25000), (45, 25040), (85, 25190), (90, 25212), (95, 25222), (100, 25185),
              (105, 25205), (115, 25250), (160, 25330), (240, 25380), (375, 25420)],
}


class SimClock:
    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, T, iv, call):
    d1 = (math.log(S / K) + 0.5 * iv * iv * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)
    return S * _ncdf(d1) - K * _ncdf(d2) if call else K * _ncdf(-d2) - S * _ncdf(-d1)


class FakeKite:
    def __init__(self, clock, points):
        self.clock = clock
        self.points = points
        self.open = datetime(DAY.year, DAY.month, DAY.day, 9, 15, tzinfo=IST)
        rnd = random.Random(7)
        self.noise = [rnd.uniform(-4, 4) for _ in range(800)]

    def spot_at(self, t):
        m = max(0.0, (t - self.open).total_seconds() / 60)
        pts = self.points
        for (m0, p0), (m1, p1) in zip(pts, pts[1:]):
            if m0 <= m <= m1:
                base = p0 + (p1 - p0) * (m - m0) / (m1 - m0)
                break
        else:
            base = pts[-1][1]
        return round(base + self.noise[int(m) % 800], 2)

    def instruments(self, exchange):
        out = []
        for k in range(23500, 26550, 50):
            for typ in ("CE", "PE"):
                out.append({"name": "NIFTY", "instrument_type": typ, "strike": float(k),
                            "expiry": EXPIRY, "lot_size": 65,
                            "tradingsymbol": f"NIFTY26OCT{k}{typ}"})
        return out

    def _oi(self, strike, typ, minute):
        if typ == "CE":
            oi = 40000 + 900000 * math.exp(-((strike - 25200) / 120) ** 2) \
                 + 300000 * math.exp(-((strike - 25500) / 100) ** 2)
            if strike == 25200:
                oi *= 1 + 0.002 * minute       # writers keep adding at 25200
        else:
            oi = 40000 + 800000 * math.exp(-((strike - 24800) / 120) ** 2)
        return int(oi)

    def quote(self, keys):
        now = self.clock.now()
        S = self.spot_at(now)
        T = max((datetime(EXPIRY.year, EXPIRY.month, EXPIRY.day, 15, 30, tzinfo=IST) - now)
                .total_seconds() / (365 * 86400), 1e-4)
        minute = (now - self.open).total_seconds() / 60
        out = {}
        for k in keys:
            if k == "NSE:NIFTY 50":
                out[k] = {"last_price": S}
                continue
            sym = k.split(":")[1]
            typ, strike = sym[-2:], float(sym[len("NIFTY26OCT"):-2])
            p = max(round(bs(S, strike, T, 0.13, typ == "CE"), 1), 0.05)
            out[k] = {"last_price": p, "oi": self._oi(strike, typ, minute),
                      "depth": {"buy": [{"price": round(p - 0.3, 2)}],
                                "sell": [{"price": round(p + 0.3, 2)}]}}
        return out

    def historical_data(self, token, start, end, interval):
        if interval == "day":
            return [{"date": start, "open": 24950, "high": 25210, "low": 24890, "close": 24995}]
        step = int(interval.replace("minute", ""))
        out, t = [], self.open
        while t < end:
            ts = [t + timedelta(minutes=i) for i in range(step + 1) if t + timedelta(minutes=i) <= end]
            px = [self.spot_at(x) for x in ts]
            out.append({"date": t, "open": px[0], "high": max(px), "low": min(px), "close": px[-1]})
            t += timedelta(minutes=step)
        return out


def main():
    # python simulate.py --date 2026-10-21   -> test the Supertrend options strategy on a real past day
    if any(a.startswith("--") for a in sys.argv[1:]):
        import daytest
        daytest.main(sys.argv[1:])
        return
    name = sys.argv[1] if len(sys.argv) > 1 else "v"
    out = f"sim_output/{name}"
    shutil.rmtree(out, ignore_errors=True)
    clock = SimClock(datetime(DAY.year, DAY.month, DAY.day, 9, 14, tzinfo=IST))
    print(f"*** SIMULATED DAY ({name}) - synthetic prices, not real market data ***\n")
    tg = alerts.Telegram.from_config(prefix="[SIMULATION] ")
    bot.run(FakeKite(clock, SCENARIOS[name]), clock, out_dir=f"{out}/logs", data_dir=f"{out}/data", tg=tg)


if __name__ == "__main__":
    main()
