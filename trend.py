"""
Trend strategy - trade WITH the move (price action + OI, no indicators).

Trend from swings (5-min candles):
  downtrend = last 2 swing highs falling (lower high)  -> buy PE
  uptrend   = last 2 swing lows rising  (higher low)   -> buy CE
  (TREND_RULE = "strict" also requires the lows/highs to agree)

Two entries (written for a downtrend; an uptrend uses the same code on a
mirrored chart, like setups.py):
  1. Breakdown : a strong red candle is the first to CLOSE below the latest swing low.
                 Stop = above the breakdown candle (and the one before it).
  2. Pullback  : after the latest lower high, price is the first to CLOSE below the
                 low of the bounce. Stop = above that lower high.

OI confirmation (scored in bot.py): put writers leaving the broken level,
call writers adding at ATM, PCR falling.
"""
from dataclasses import dataclass

import config as C
from setups import _flip, _swing_highs

# Settings - defaults used if they are not in config.py
TREND_ENABLED = getattr(C, "TREND_ENABLED", True)
TREND_MIN_SCORE = getattr(C, "TREND_MIN_SCORE", 3)
TREND_TARGET_R = getattr(C, "TREND_TARGET_R", 2.0)        # trends run: target 2 x risk
TREND_TRAIL_AFTER_R = getattr(C, "TREND_TRAIL_AFTER_R", 1.0)  # start trailing after 1 x risk
TREND_MAX_PER_DIRECTION = getattr(C, "TREND_MAX_PER_DIRECTION", 1)
TREND_BODY_RATIO = getattr(C, "TREND_BODY_RATIO", 0.5)    # trigger candle body >= 50% of range
BLOCK_COUNTER_TREND = getattr(C, "BLOCK_COUNTER_TREND", True)  # no reversal trades against the trend
# "strict" = lower highs AND lower lows.  "highs" = lower highs only (downtrend) /
# higher lows only (uptrend) - the classic "lower high, then support breaks" pattern.
TREND_RULE = getattr(C, "TREND_RULE", "highs")


@dataclass
class TrendSetup:
    name: str
    sl: float
    level: float     # the level that was broken
    note: str


def _swing_lows(c):
    return [(i, -v) for i, v in _swing_highs(_flip(c))]


def _is_down(c):
    hi, lo = _swing_highs(c), _swing_lows(c)
    if TREND_RULE == "highs":
        return len(hi) >= 2 and len(lo) >= 1 and hi[-1][1] < hi[-2][1]
    if len(hi) < 2 or len(lo) < 2:
        return False
    return hi[-1][1] < hi[-2][1] and lo[-1][1] < lo[-2][1]


def trend_direction(candles):
    """'down', 'up' or None."""
    if len(candles) < 8:
        return None
    if _is_down(candles):
        return "down"
    if _is_down(_flip(candles)):
        return "up"
    return None


def _down_setups(c):
    out = []
    if len(c) < 8 or not _is_down(c):
        return out
    last, prev = c[-1], c[-2]
    rng = last["high"] - last["low"]
    strong_red = (rng > 0 and last["close"] < last["open"]
                  and (last["open"] - last["close"]) >= TREND_BODY_RATIO * rng)
    if not strong_red:
        return out
    buf = C.SL_BUFFER_POINTS
    highs, lows = _swing_highs(c), _swing_lows(c)

    # 1. Breakdown below the latest swing low
    i_l, L = lows[-1]
    if prev["close"] >= L > last["close"]:
        sl = max(last["high"], prev["high"]) + buf
        out.append(TrendSetup("trend breakdown", sl, L,
                              f"closed below swing low {abs(L):.0f}"))

    # 2. Pullback: lower high formed after the latest swing low, then its bounce low breaks
    i_h, H = highs[-1]
    if i_h > i_l and i_h < len(c) - 1:
        trig = min(x["low"] for x in c[i_h:-1])
        if prev["close"] >= trig > last["close"]:
            out.append(TrendSetup("trend pullback", H + buf, trig,
                                  f"lower high {abs(H):.0f} rejected, broke {abs(trig):.0f}"))
    return out


NAMES_UP = {"trend breakdown": "trend breakout", "trend pullback": "trend pullback"}


def find_trend_setups(candles):
    """Returns (direction, [TrendSetup]) with direction 'PE', 'CE' or None."""
    if not TREND_ENABLED:
        return None, []
    tr = trend_direction(candles)
    if tr == "down":
        return "PE", _down_setups(candles)
    if tr == "up":
        ups = _down_setups(_flip(candles))
        return "CE", [TrendSetup(NAMES_UP[s.name], -s.sl, -s.level,
                                 s.note.replace("below", "above").replace("low", "high", 1)
                                 if s.name == "trend breakdown" else
                                 s.note.replace("lower high", "higher low"))
                      for s in ups]
    return None, []


def trend_label(tr):
    if tr == "down":
        return "downtrend (lower highs)" if TREND_RULE == "highs" else "downtrend (lower highs + lower lows)"
    return "uptrend (higher lows)" if TREND_RULE == "highs" else "uptrend (higher highs + higher lows)"
