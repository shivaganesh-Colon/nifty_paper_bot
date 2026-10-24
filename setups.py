"""
Price-action setups - the "WHEN". No indicators, only highs, lows and closes.

All four setups are written once for a RESISTANCE level (turn down -> buy PE).
For a SUPPORT level the candles are mirrored (prices flipped), so the exact
same rules find the turn up (-> buy CE). Same logic both ways, no duplicate code.

  1. Failed breakout  - closes above the level, then closes back below it
  2. Double top       - two tops at the level, then breaks the dip between them
  3. Lower high       - top at the level, weaker second high, breaks the pullback low
  4. Rejection candle - long upper wick at the level, next candle breaks its low
"""
from dataclasses import dataclass

import config as C

MIRROR_NAMES = {"double top": "double bottom", "lower high": "higher low"}


@dataclass
class Setup:
    name: str
    sl: float      # NIFTY level that proves the idea wrong
    note: str


def _flip(candles):
    return [{"time": x["time"], "open": -x["open"], "high": -x["low"],
             "low": -x["high"], "close": -x["close"]} for x in candles]


def _swing_highs(c):
    k = C.SWING_STRENGTH
    return [(i, c[i]["high"]) for i in range(k, len(c) - k)
            if c[i]["high"] == max(x["high"] for x in c[i - k:i + k + 1])]


def _find_down(c, L):
    """Setups for a turn DOWN at level L. Returns a list of Setup."""
    out = []
    if len(c) < 6:
        return out
    last, prev = c[-1], c[-2]
    near, buf = C.NEAR_LEVEL_POINTS, C.SL_BUFFER_POINTS

    # 1. Failed breakout: a close beyond L, then the FIRST close back inside
    w = c[-(C.FAILED_BREAKOUT_CANDLES + 1):-1]
    if (any(x["close"] >= L + C.BREAKOUT_MIN_POINTS for x in w)
            and prev["close"] >= L and last["close"] < L and last["close"] < last["open"]):
        ext = max(x["high"] for x in w + [last])
        out.append(Setup("failed breakout", ext + buf,
                         f"closed beyond {abs(L):.0f}, then back inside; extreme {abs(ext):.0f}"))

    # 2 & 3. Patterns built from the last two swing highs
    highs = [s for s in _swing_highs(c) if s[0] >= len(c) - C.PATTERN_LOOKBACK]
    if len(highs) >= 2:
        (i1, h1), (i2, h2) = highs[-2], highs[-1]
        if i2 - i1 >= 3:
            neck = min(x["low"] for x in c[i1:i2 + 1])
            fresh_break = prev["close"] >= neck and last["close"] < neck
            top = max(h1, h2)
            if (abs(h1 - h2) <= C.DOUBLE_TOP_TOLERANCE and abs(top - L) <= near
                    and top - neck >= C.DOUBLE_TOP_MIN_DIP and fresh_break):
                out.append(Setup("double top", top + buf,
                                 f"two extremes ~{abs(top):.0f}, broke {abs(neck):.0f}"))
            elif h2 <= h1 - C.LOWER_HIGH_MIN_POINTS and abs(h1 - L) <= near and fresh_break:
                out.append(Setup("lower high", h2 + buf,
                                 f"extreme {abs(h1):.0f}, weaker {abs(h2):.0f}, broke {abs(neck):.0f}"))

    # 4. Rejection candle at the level, confirmed by the next candle
    rng = prev["high"] - prev["low"]
    if rng > 0 and abs(prev["high"] - L) <= near:
        wick = prev["high"] - max(prev["open"], prev["close"])
        if (wick >= C.WICK_RATIO * rng and prev["close"] - prev["low"] <= 0.4 * rng
                and last["close"] < prev["low"]):
            out.append(Setup("rejection candle", prev["high"] + buf,
                             f"long wick to {abs(prev['high']):.0f}, next candle broke it"))
    return out


def find_setups(candles, level, side):
    """side='resistance' -> turn down (PE).  side='support' -> turn up (CE)."""
    if side == "resistance":
        return _find_down(candles, level)
    return [Setup(MIRROR_NAMES.get(s.name, s.name), -s.sl, s.note)
            for s in _find_down(_flip(candles), -level)]


def move_into(candles, side):
    """Size (points) of the most recent push INTO the level."""
    c = candles if side == "resistance" else _flip(candles)
    w = c[-C.STRONG_MOVE_CANDLES:]
    if not w:
        return 0
    p = max(range(len(w)), key=lambda i: w[i]["high"])
    return w[p]["high"] - min(x["low"] for x in w[:p + 1])
