"""
Supertrend OPTIONS strategy (paper), 5-min NIFTY candles:
  Fresh flip to GREEN -> exit any PE, buy ATM CE
  Fresh flip to RED   -> exit any CE, buy ATM PE
  (at 09:20 the first trade follows the current colour)
Exit, whichever comes first:
  - STOP   : NIFTY moves ST_STOP_POINTS against us (checked every 2 seconds)
  - TARGET : NIFTY moves ST_TARGET_POINTS in our favour (checked every 2 seconds)
  - TRAIL  : after +ST_TRAIL_START pts, stop follows ST_TRAIL_POINTS behind the best price
             (moved at each 5-min close, checked every 2 seconds)
  - FLIP   : Supertrend turns the other way at a 5-min close
  - TIME   : EXIT_TIME (15:15)
After a target exit, no new trade until the next fresh flip.
New entries 09:20-15:00.

Backtest (Oct 2025 - Oct 2026, ATM options with realistic time decay & costs, 1 lot):
  Supertrend(10,5) + 60-pt target:                 about +Rs 24k for the year (Aug-Oct: -Rs 2k)
  Supertrend(10,5) + 60-pt target + trail 40 after +40: about +Rs 47k (Aug-Oct: +Rs 3.4k),
      but worst single trade -Rs 10.5k
  ... + 60-pt initial stop-loss (current default): about +Rs 29k (Aug-Oct: +Rs 14k),
      worst single trade -Rs 2.6k
  A thin edge from one (trending) year - paper-test before trusting it.
"""
import config as C

ST_ENABLED = getattr(C, "ST_ENABLED", True)
ST_PERIOD = getattr(C, "ST_PERIOD", 10)          # ATR period
ST_MULT = getattr(C, "ST_MULT", 5.0)             # ATR multiplier (5 = slow, fewer whipsaws)
ST_TARGET_POINTS = getattr(C, "ST_TARGET_POINTS", 60)   # NIFTY points; 0 = no target (exit on flip only)
# Trailing stop: once NIFTY is ST_TRAIL_START pts in our favour, the stop follows
# ST_TRAIL_POINTS behind the best price (at +40 with 40 behind = stop at entry). 0 = off.
ST_TRAIL_START = getattr(C, "ST_TRAIL_START", 40)
ST_TRAIL_POINTS = getattr(C, "ST_TRAIL_POINTS", 40)
# Initial stop-loss: exit if NIFTY moves this many points against the trade (0 = off).
# Added after Oct 19 2026: a CE bought at 9:20 fell 171 pts in one candle before the flip.
ST_STOP_POINTS = getattr(C, "ST_STOP_POINTS", 60)
# When the trailing stop moves: "candle" = at each 5-min close (default, suits wide trails),
# "tick" = continuously as the price moves (needed for tight trails like 5 pts).
ST_TRAIL_MODE = getattr(C, "ST_TRAIL_MODE", "candle")
# First trade at 9:20 follows the current colour (False = only trade fresh flips)
ST_FIRST_TRADE = getattr(C, "ST_FIRST_TRADE", True)
ST_START = getattr(C, "ST_START", "09:20")       # first entry (first 5-min candle closed)
ST_LAST_ENTRY = getattr(C, "ST_LAST_ENTRY", "15:00")
ST_WARMUP_DAYS = getattr(C, "ST_WARMUP_DAYS", 10)  # past days loaded so the line is ready at 9:20


def compute(candles, period=None, mult=None):
    """Returns (directions, lines): direction +1 green / -1 red / None (not ready), and the line value."""
    period = period or ST_PERIOD
    mult = mult or ST_MULT
    trs, dirs, lines = [], [], []
    atr = fu = fl = None
    d = 1
    for i, b in enumerate(candles):
        pc = candles[i - 1]["close"] if i else b["close"]
        tr = max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc))
        trs.append(tr)
        if i < period:
            dirs.append(None)
            lines.append(None)
            continue
        atr = sum(trs[-period:]) / period if atr is None else (atr * (period - 1) + tr) / period
        mid = (b["high"] + b["low"]) / 2
        ub, lb = mid + mult * atr, mid - mult * atr
        fu = ub if (fu is None or ub < fu or pc > fu) else fu
        fl = lb if (fl is None or lb > fl or pc < fl) else fl
        if d == 1 and b["close"] < fl:
            d = -1
        elif d == -1 and b["close"] > fu:
            d = 1
        dirs.append(d)
        lines.append(fl if d == 1 else fu)
    return dirs, lines


def latest(candles):
    """(direction, line) at the last completed candle, or (None, None)."""
    if not candles:
        return None, None
    dirs, lines = compute(candles)
    return dirs[-1], lines[-1]
