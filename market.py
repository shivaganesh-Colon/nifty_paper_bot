"""
Market data from Zerodha: option contracts, live quotes, NIFTY candles.
Read-only - nothing in this file places orders.
"""
import logging
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import config as C

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("bot")


def hhmm(text):
    h, m = map(int, text.split(":"))
    return dtime(h, m)


class Clock:
    """Real clock. (The simulator swaps in a fake one.)"""
    def now(self):
        return datetime.now(IST)

    def sleep(self, seconds):
        time.sleep(seconds)


class Market:
    def __init__(self, kite, clock):
        self.kite = kite
        self.clock = clock
        self._last_quote_call = None

    # Zerodha allows ~1 quote request per second
    def _throttle(self):
        now = self.clock.now()
        if self._last_quote_call is not None:
            gap = (now - self._last_quote_call).total_seconds()
            if gap < 1.05:
                self.clock.sleep(1.05 - gap)
        self._last_quote_call = self.clock.now()

    def quote(self, keys):
        out = {}
        for i in range(0, len(keys), 500):
            self._throttle()
            out.update(self.kite.quote(keys[i:i + 500]))
        return out

    def load_options(self, today):
        """Returns (expiry, {(strike, 'CE'/'PE'): instrument}, lot_size)."""
        inst = self.kite.instruments("NFO")
        opts = [i for i in inst
                if i["name"] == C.UNDERLYING and i["instrument_type"] in ("CE", "PE")]
        expiries = sorted({i["expiry"] for i in opts if i["expiry"] >= today})
        if not expiries:
            raise RuntimeError("No NIFTY option expiries found")
        expiry = expiries[0]
        if expiry == today and not C.TRADE_ON_EXPIRY_DAY and len(expiries) > 1:
            expiry = expiries[1]
        chain = {(float(i["strike"]), i["instrument_type"]): i
                 for i in opts if i["expiry"] == expiry}
        lot_size = next(iter(chain.values()))["lot_size"]
        return expiry, chain, lot_size

    def candles(self, today):
        """Today's COMPLETED NIFTY candles (the still-forming candle is dropped)."""
        start = datetime.combine(today, hhmm(C.MARKET_OPEN), IST)
        now = self.clock.now()
        data = self.kite.historical_data(C.SPOT_TOKEN, start, now,
                                         f"{C.CANDLE_MINUTES}minute")
        step = timedelta(minutes=C.CANDLE_MINUTES)
        out = []
        for c in data:
            t = c["date"]
            if t.tzinfo is None:
                t = t.replace(tzinfo=IST)
            if t + step <= now:
                out.append({"time": t, "open": c["open"], "high": c["high"],
                            "low": c["low"], "close": c["close"]})
        return out

    def history(self, today, days):
        """Completed NIFTY candles from the previous `days` calendar days (for indicator warm-up)."""
        start = datetime.combine(today - timedelta(days=days), hhmm(C.MARKET_OPEN), IST)
        end = datetime.combine(today - timedelta(days=1), dtime(23, 59), IST)
        try:
            data = self.kite.historical_data(C.SPOT_TOKEN, start, end, f"{C.CANDLE_MINUTES}minute")
        except Exception as e:
            log.warning("Could not load history for warm-up: %s", e)
            return []
        return [{"time": c["date"], "open": c["open"], "high": c["high"],
                 "low": c["low"], "close": c["close"]} for c in data]

    def prev_day(self, today):
        """Previous trading day's high / low / close."""
        start = datetime.combine(today - timedelta(days=10), dtime(9, 0), IST)
        end = datetime.combine(today - timedelta(days=1), dtime(23, 59), IST)
        try:
            data = self.kite.historical_data(C.SPOT_TOKEN, start, end, "day")
        except Exception as e:
            log.warning("Could not load previous day data: %s", e)
            return None
        if not data:
            return None
        d = data[-1]
        return {"high": d["high"], "low": d["low"], "close": d["close"]}


def best_prices(q):
    """(bid, ask, ltp) from a Kite quote."""
    ltp = q.get("last_price", 0) or 0
    depth = q.get("depth") or {}
    try:
        bid = depth["buy"][0]["price"] or ltp
    except (KeyError, IndexError):
        bid = ltp
    try:
        ask = depth["sell"][0]["price"] or ltp
    except (KeyError, IndexError):
        ask = ltp
    return bid, ask, ltp
