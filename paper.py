"""
Paper trade engine. Simulates buying and selling options at real bid/ask
prices (plus slippage) and logs every trade. No real orders, ever.

Exits (whichever comes first):
  - Stop-loss  : NIFTY crosses the setup's extreme (the reversal idea failed)
  - Safety SL  : option premium falls PREMIUM_SL_PCT
  - Target     : NIFTY moves TARGET_R x risk in our favour
                 (1 lot: exit fully. 2+ lots: book half, trail the rest from cost)
  - Breakeven  : once NIFTY moves BREAKEVEN_AT_R x risk in our favour, stop moves to entry
  - Time stop  : after TIME_STOP_MINUTES, exit if NIFTY hasn't moved TIME_STOP_MIN_R x risk
                 in our favour (a flat option just loses value to time decay)
  - Time exit  : EXIT_TIME
"""
import csv
import os
from dataclasses import dataclass, field

import config as C

# New settings - defaults used if they are not in config.py. Set to 0 to switch off.
BREAKEVEN_AT_R = getattr(C, "BREAKEVEN_AT_R", 0.6)
TIME_STOP_MINUTES = getattr(C, "TIME_STOP_MINUTES", 45)
TIME_STOP_MIN_R = getattr(C, "TIME_STOP_MIN_R", 0.3)
TREND_TRAIL_POINTS = getattr(C, "TREND_TRAIL_POINTS", 30)   # trailing distance for trend trades

TRADE_FIELDS = ["date", "entry_time", "exit_time", "direction", "symbol", "setup",
                "level", "score", "reasons", "lots", "qty", "entry_spot", "sl_spot",
                "target_spot", "exit_spot", "entry_premium", "exit_premium",
                "exit_reason", "gross_pnl", "est_charges", "net_pnl", "result"]


@dataclass
class Trade:
    direction: str          # "CE" (expect up) or "PE" (expect down)
    symbol: str
    lot_size: int
    lots: int
    entry_time: object
    entry_spot: float
    entry_premium: float
    sl_spot: float
    target_spot: float
    setup: str
    level: float
    score: int
    reasons: str
    lots_open: int = 0
    booked: float = 0.0
    best_spot: float = 0.0
    target_done: bool = False
    risk: float = 0.0       # NIFTY points from entry to the original stop
    be_done: bool = False   # stop already moved to entry
    trail_r: float = 0.0    # trend trades: start trailing after this x risk (0 = never)
    trailing: bool = False
    exits: list = field(default_factory=list)

    @property
    def sgn(self):
        return 1 if self.direction == "CE" else -1


def _sell_price(bid, ltp):
    p = bid if bid > 0 else ltp
    return round(p * (1 - C.PAPER_SLIPPAGE_PCT / 100), 2)


def buy_price(ask, ltp):
    p = ask if ask > 0 else ltp
    return round(p * (1 + C.PAPER_SLIPPAGE_PCT / 100), 2)


class PaperBook:
    def __init__(self, trades_file, log):
        self.file = trades_file
        self.log = log

    def open(self, **kw):
        t = Trade(**kw)
        t.lots_open = t.lots
        t.best_spot = t.entry_spot
        t.risk = abs(t.entry_spot - t.sl_spot)
        self.log.info("PAPER BUY %s x%d @ %.2f | NIFTY %.1f | SL %.1f | target %.1f | %s",
                      t.symbol, t.lots * t.lot_size, t.entry_premium, t.entry_spot,
                      t.sl_spot, t.target_spot, t.reasons)
        return t

    def _exit(self, t, lots, now, spot, price, reason):
        pnl = (price - t.entry_premium) * lots * t.lot_size
        t.booked += pnl
        t.lots_open -= lots
        t.exits.append((now, spot, price, reason, lots))
        self.log.info("PAPER SELL %s x%d @ %.2f | NIFTY %.1f | %s | P&L Rs %.0f",
                      t.symbol, lots * t.lot_size, price, spot, reason, pnl)

    def manage(self, t, now, spot, bid, ltp):
        """Check exits. Returns True when the trade is fully closed."""
        s = t.sgn
        if s * (spot - t.best_spot) > 0:
            t.best_spot = spot
        if t.target_done:   # trail the runner
            trail = t.best_spot - s * C.TRAIL_POINTS
            if s * (trail - t.sl_spot) > 0:
                t.sl_spot = trail
        progress = s * (spot - t.entry_spot)          # NIFTY points in our favour
        if (BREAKEVEN_AT_R > 0 and not t.be_done and not t.target_done and t.risk > 0
                and progress >= BREAKEVEN_AT_R * t.risk):
            t.be_done = True
            t.sl_spot = t.entry_spot
            self.log.info("BREAKEVEN: NIFTY moved %.0f pts in favour - stop moved to entry %.1f",
                          progress, t.entry_spot)
        if t.trail_r > 0 and t.risk > 0 and not t.trailing and progress >= t.trail_r * t.risk:
            t.trailing = True
            self.log.info("TRAILING ON: stop now follows %d pts behind the best NIFTY price",
                          TREND_TRAIL_POINTS)
        if t.trailing:
            trail = t.best_spot - s * TREND_TRAIL_POINTS
            if s * (trail - t.sl_spot) > 0:
                t.sl_spot = trail
        price = _sell_price(bid, ltp)
        mins = (now - t.entry_time).total_seconds() / 60

        if s * (spot - t.sl_spot) <= 0:
            why = ("trailing stop" if (t.target_done or t.trailing) else
                   "breakeven stop" if t.be_done else "stop-loss (NIFTY level)")
            self._exit(t, t.lots_open, now, spot, price, why)
        elif price <= t.entry_premium * (1 - C.PREMIUM_SL_PCT / 100):
            self._exit(t, t.lots_open, now, spot, price, "premium safety SL")
        elif not t.target_done and s * (spot - t.target_spot) >= 0:
            if t.lots_open >= 2:
                self._exit(t, t.lots_open // 2, now, spot, price, "target (half booked)")
                t.target_done = True
                t.sl_spot = t.entry_spot          # rest is now risk-free (stop at cost)
            else:
                self._exit(t, t.lots_open, now, spot, price, "target")
        elif (TIME_STOP_MINUTES > 0 and not t.target_done and mins >= TIME_STOP_MINUTES
              and progress < TIME_STOP_MIN_R * t.risk):
            self._exit(t, t.lots_open, now, spot, price,
                       f"time stop ({TIME_STOP_MINUTES} min, no move)")
        return t.lots_open == 0

    def close(self, t, now, spot, bid, ltp, reason):
        if t.lots_open > 0:
            self._exit(t, t.lots_open, now, spot, _sell_price(bid, ltp), reason)

    def record(self, t):
        charges = C.EST_CHARGES_PER_TRADE * (1 + max(0, len(t.exits) - 1) * 0.5)
        net = t.booked - charges
        last = t.exits[-1]
        row = {
            "date": t.entry_time.date().isoformat(),
            "entry_time": t.entry_time.strftime("%H:%M:%S"),
            "exit_time": last[0].strftime("%H:%M:%S"),
            "direction": t.direction, "symbol": t.symbol, "setup": t.setup,
            "level": t.level, "score": t.score, "reasons": t.reasons,
            "lots": t.lots, "qty": t.lots * t.lot_size,
            "entry_spot": round(t.entry_spot, 1), "sl_spot": round(t.sl_spot, 1),
            "target_spot": round(t.target_spot, 1), "exit_spot": round(last[1], 1),
            "entry_premium": t.entry_premium, "exit_premium": last[2],
            "exit_reason": " + ".join(e[3] for e in t.exits),
            "gross_pnl": round(t.booked, 2), "est_charges": round(charges, 2),
            "net_pnl": round(net, 2), "result": "WIN" if net > 0 else "LOSS",
        }
        new = not os.path.exists(self.file)
        with open(self.file, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)
        self.log.info("TRADE CLOSED: %s | net P&L Rs %.0f (after est. charges)", row["result"], net)
        return net
