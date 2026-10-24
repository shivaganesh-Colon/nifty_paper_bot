"""
Telegram alerts. Messages are sent in the background, so a slow or failed
Telegram call never delays the bot. If Telegram is off or not set up,
everything here silently does nothing.

Set up once with:  python telegram_setup.py
"""
import logging
import queue
import threading
import time

import requests

import config as C
import supertrend as STM

log = logging.getLogger("bot")
API = "https://api.telegram.org/bot{}/{}"


class Telegram:
    def __init__(self, token, chat_id, enabled=True, prefix=""):
        token, chat_id = str(token or ""), str(chat_id or "")
        self.enabled = bool(enabled and token and chat_id and not token.startswith("your_"))
        self.token, self.chat_id, self.prefix = token, chat_id, prefix
        self.q = queue.Queue()
        if self.enabled:
            threading.Thread(target=self._worker, daemon=True).start()

    @classmethod
    def from_config(cls, prefix=""):
        return cls(getattr(C, "TELEGRAM_BOT_TOKEN", ""), getattr(C, "TELEGRAM_CHAT_ID", ""),
                   getattr(C, "TELEGRAM_ENABLED", False), prefix)

    def send(self, text):
        if self.enabled:
            self.q.put(self.prefix + text)

    def _worker(self):
        while True:
            text = self.q.get()
            for attempt in range(3):
                try:
                    self._post(text)
                    break
                except Exception as e:
                    if attempt == 2:
                        log.warning("Telegram alert failed: %s", e)
                    else:
                        time.sleep(2)
            self.q.task_done()

    def _post(self, text):
        r = requests.post(API.format(self.token, "sendMessage"),
                          data={"chat_id": self.chat_id, "text": text,
                                "disable_web_page_preview": True},
                          timeout=10)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")

    def flush(self, timeout=20):
        """Wait (up to `timeout` s) for queued messages to go out - used before exit."""
        if not self.enabled:
            return
        end = time.time() + timeout
        while self.q.unfinished_tasks and time.time() < end:
            time.sleep(0.2)


# ---------------- message texts ----------------

def _rs(x):
    return f"₹{x:,.2f}"


def start_msg(expiry, lot_size):
    return (f"🤖 NIFTY paper bot started\n"
            f"Expiry: {expiry:%d %b %Y} | Lot size: {lot_size} | Lots: {C.LOTS}\n"
            f"New trades: {C.NO_TRADE_BEFORE}-{C.NO_NEW_TRADE_AFTER} | Exit by {C.EXIT_TIME}")


def levels_msg(now, spot, res, sup):
    return (f"📊 OI levels at {now:%H:%M} (NIFTY {spot:,.1f})\n"
            f"Resistance (Call OI): {', '.join(f'{x:,.0f}' for x in res) or '-'}\n"
            f"Support (Put OI): {', '.join(f'{x:,.0f}' for x in sup) or '-'}")


def entry_msg(t):
    up = t.direction == "CE"
    reasons = "\n• ".join(t.reasons.split("; "))
    return (f"🟢 PAPER BUY  {t.symbol}\n"
            f"View: NIFTY to turn {'UP' if up else 'DOWN'}\n"
            f"Entry premium: {_rs(t.entry_premium)}  (qty {t.lots * t.lot_size})\n"
            f"NIFTY now: {t.entry_spot:,.1f}\n"
            f"Stop-loss: NIFTY {'below' if up else 'above'} {t.sl_spot:,.1f}\n"
            f"Target: NIFTY {'at/above' if up else 'at/below'} {t.target_spot:,.1f}\n"
            f"Level: {t.level:,.0f} | Score: {t.score}\n"
            f"Why:\n• {reasons}\n"
            f"Time: {t.entry_time:%H:%M}")


def st_entry_msg(t, line):
    up = t.direction == "CE"
    return (f"🟢 PAPER BUY  {t.symbol}  (Supertrend)\n"
            f"Supertrend turned {'GREEN (up)' if up else 'RED (down)'} at 5-min close\n"
            f"Entry premium: {_rs(t.entry_premium)}  (qty {t.lots * t.lot_size})\n"
            f"NIFTY now: {t.entry_spot:,.1f} | Supertrend line: {line:,.1f}\n"
            + (f"Target: NIFTY {'at/above' if up else 'at/below'} "
               f"{t.target_spot:,.1f} (+{abs(t.target_spot - t.entry_spot):.0f} pts)\n" if t.target_spot else "")
            + (f"Stop-loss: NIFTY {'below' if up else 'above'} "
               f"{t.entry_spot - (1 if up else -1) * STM.ST_STOP_POINTS:,.1f} (-{STM.ST_STOP_POINTS} pts)\n"
               if STM.ST_STOP_POINTS else "")
            + (f"Trailing stop: after +{STM.ST_TRAIL_START} pts, follows {STM.ST_TRAIL_POINTS} pts behind\n"
               if STM.ST_TRAIL_POINTS else "")
            + f"Exit: target, trail, or Supertrend turns {'RED' if up else 'GREEN'} (5-min close), or {C.EXIT_TIME}\n"
            f"Time: {t.entry_time:%H:%M}")


def partial_msg(t, exit_):
    when, spot, price, reason, lots = exit_
    pnl = (price - t.entry_premium) * lots * t.lot_size
    return (f"🟡 PAPER PARTIAL EXIT  {t.symbol}\n"
            f"{reason}: sold {lots * t.lot_size} @ {_rs(price)} (P&L {_rs(pnl)})\n"
            f"Rest: stop moved to cost, trailing | NIFTY {spot:,.1f} | {when:%H:%M}")


def exit_msg(t, net):
    when, spot, price, _, _ = t.exits[-1]
    reason = " + ".join(e[3] for e in t.exits)
    icon = "✅" if net > 0 else "🔴"
    qty = t.lots * t.lot_size
    opt_pts = t.booked / qty if qty else 0                       # avg premium points per unit
    nifty_pts = (spot - t.entry_spot) * (1 if t.direction == "CE" else -1)   # in our favour
    mins = (when - t.entry_time).total_seconds() / 60
    return (f"{icon} PAPER EXIT  {t.symbol}\n"
            f"Reason: {reason}\n"
            f"Option points: {opt_pts:+.2f}  ({_rs(t.entry_premium)} → {_rs(price)})\n"
            f"NIFTY points: {nifty_pts:+.1f}  ({t.entry_spot:,.1f} → {spot:,.1f})\n"
            f"P&L: {'+' if net > 0 else '-'}₹{abs(net):,.0f} for qty {qty} (after est. charges)\n"
            f"Held: {mins:.0f} min | Exit time: {when:%H:%M}")


def summary_msg(day_date, trades, pnl):
    return (f"📋 Day finished {day_date:%d %b}\n"
            f"Paper trades: {trades} | P&L: {'+' if pnl > 0 else '-' if pnl < 0 else ''}₹{abs(pnl):,.0f}\n"
            f"Run  python report.py  for full details.")
