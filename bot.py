"""
NIFTY option-buying PAPER TRADING bot.
OI levels = WHERE to trade. Price action = WHEN to trade.

Run every trading day:
    python login.py     (once in the morning, before 9:15)
    python bot.py       (leave it running; it stops itself after 15:15)

Stop early at any time: press Ctrl+C, or create a file named STOP in this folder.
Any open paper trade is closed and logged first.
Telegram alerts (optional): set up once with  python telegram_setup.py
"""
import csv
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime

import alerts
import config as C
import supertrend as STM
import trend as T
from levels import ChainTracker
from market import IST, Clock, Market, best_prices, hhmm
from paper import PaperBook, buy_price
from report import report
from setups import find_setups, move_into

log = logging.getLogger("bot")
SIGNAL_FIELDS = ["time", "direction", "setup", "level", "score", "reasons", "decision"]


class ClockFormatter(logging.Formatter):
    """Log timestamps from the bot's clock (real time live, simulated time in simulate.py)."""
    def __init__(self, clock):
        super().__init__("%(asctime)s  %(message)s")
        self.clock = clock

    def formatTime(self, record, datefmt=None):
        return self.clock.now().strftime("%H:%M:%S")


def setup_logging(out_dir, day, clock):
    os.makedirs(out_dir, exist_ok=True)
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = ClockFormatter(clock)
    for h in (logging.FileHandler(f"{out_dir}/bot_{day}.log", encoding="utf-8"),
              logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        log.addHandler(h)


def make_kite():
    from kiteconnect import KiteConnect
    try:
        with open("access_token.json") as f:
            tok = json.load(f)
    except FileNotFoundError:
        sys.exit("No login found. Run:  python login.py")
    if tok["date"] != datetime.now(IST).date().isoformat():
        sys.exit("Login is from an earlier day. Run:  python login.py")
    kite = KiteConnect(api_key=C.API_KEY)
    kite.set_access_token(tok["access_token"])
    return kite


def log_signal(path, now, direction, setup, level, score, reasons, decision):
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(SIGNAL_FIELDS)
        w.writerow([now.strftime("%Y-%m-%d %H:%M"), direction, setup, level, score, reasons, decision])


class Day:
    """Daily counters and limits."""
    def __init__(self):
        self.trades = 0
        self.losses = 0
        self.pnl = 0.0
        self.by_dir = defaultdict(int)     # (strategy, direction) -> trades today
        self.blocked = None

    def register(self, sig):
        self.trades += 1
        self.by_dir[(sig.get("strategy", "reversal"), sig["direction"])] += 1
        self.check()

    def check(self):
        if self.trades >= C.MAX_TRADES_PER_DAY:
            self.blocked = "max trades for the day reached"
        elif self.losses >= C.MAX_LOSSES_PER_DAY:
            self.blocked = "max losses for the day reached"
        elif self.pnl <= -C.MAX_DAILY_LOSS_RUPEES:
            self.blocked = "max daily loss reached"


def evaluate(now, candles, spot, tracker, prev, day, signals_file, tg=None):
    """Find setups at OI levels, score them, return the best tradeable one (or None)."""
    res, sup = tracker.levels(spot)
    pcr = tracker.pcr_trend(C.OI_TREND_MINUTES)
    trend_now = T.trend_direction(candles)
    best = None

    def consider(cand):
        nonlocal best
        if best is None or (cand["score"], -cand["risk"]) > (best["score"], -best["risk"]):
            best = cand

    def record(direction, name, L, score, reasons, decision):
        log.info("Signal %s %s at %.0f | score %d | %s", direction, name, L, score, decision)
        log_signal(signals_file, now, direction, name, L, score, "; ".join(reasons), decision)
        if tg and decision.startswith("SKIP") and getattr(C, "ALERT_SKIPPED_SIGNALS", False):
            tg.send(f"⚪ Signal skipped: {direction} {name} at {L:,.0f} "
                    f"(score {score})\n{decision[6:]}\nTime: {now:%H:%M}")

    # ---------- trend strategy: trade WITH the move ----------
    t_dir, t_setups = T.find_trend_setups(candles)
    step = C.STRIKE_STEP
    for s in t_setups:
        score, reasons = 1, [s.name, s.note, T.trend_label(trend_now)]
        score += 1   # strong trigger candle (required by trend.py)
        reasons.append("strong candle close")
        down = t_dir == "PE"
        lvl_strike = (-(-s.level // step) * step) if down else (s.level // step * step)
        fled_type, add_type = ("PE", "CE") if down else ("CE", "PE")
        st, ch = tracker.writers_status(lvl_strike, fled_type)
        if st == "exiting":
            score += 1
            reasons.append(f"{fled_type} writers leaving {lvl_strike:.0f} ({ch:+.1f}% OI)")
        atm = round(spot / step) * step
        st2, ch2 = tracker.writers_status(atm, add_type)
        if st2 == "adding":
            score += 1
            reasons.append(f"{add_type} writers adding at ATM {atm:.0f} ({ch2:+.1f}% OI)")
        if pcr is not None and ((not down and pcr >= C.PCR_TREND_MIN) or (down and pcr <= -C.PCR_TREND_MIN)):
            score += 1
            reasons.append(f"PCR trend {pcr:+.2f} agrees")
        risk = (s.sl - spot) * (1 if down else -1)
        if risk <= 0:
            decision = "SKIP: price already beyond stop"
        elif risk < C.MIN_RISK_POINTS:
            decision = f"SKIP: stop too tight ({risk:.0f} pts)"
        elif risk > C.MAX_RISK_POINTS:
            decision = f"SKIP: stop too far ({risk:.0f} pts)"
        elif score < T.TREND_MIN_SCORE:
            decision = f"SKIP: score {score} < {T.TREND_MIN_SCORE}"
        elif day.by_dir[("trend", t_dir)] >= T.TREND_MAX_PER_DIRECTION:
            decision = f"SKIP: already took a trend {t_dir} today"
        else:
            decision = "CANDIDATE"
            consider(dict(direction=t_dir, setup=s.name, level=s.level, score=score, sl=s.sl,
                          risk=risk, reasons="; ".join(reasons), strategy="trend",
                          target_r=T.TREND_TARGET_R, trail_r=T.TREND_TRAIL_AFTER_R))
        record(t_dir, s.name, s.level, score, reasons, decision)

    # ---------- reversal strategy: trade the turn at OI walls ----------
    reversal_on = getattr(C, "REVERSAL_ENABLED", True)
    for side, levels, direction, writer_type in ((("resistance", res, "PE", "CE"),
                                                  ("support", sup, "CE", "PE")) if reversal_on else ()):
        for L in levels:
            for s in find_setups(candles, L, side):
                score, reasons = 1, [s.name, s.note]
                status, ch = tracker.writers_status(L, writer_type)
                if status in ("holding", "adding"):
                    score += 1 if status == "holding" else 2
                    reasons.append(f"{writer_type} writers {status} at {L:.0f} ({ch:+.1f}% OI)")
                mv = move_into(candles, side)
                if mv >= C.STRONG_MOVE_POINTS:
                    score += 1
                    reasons.append(f"strong {mv:.0f}-pt move into level")
                if prev:
                    pd = prev["high"] if side == "resistance" else prev["low"]
                    if abs(L - pd) <= C.NEAR_LEVEL_POINTS:
                        score += 1
                        reasons.append(f"prev-day {'high' if side == 'resistance' else 'low'} {pd:.0f} at level")
                if pcr is not None and ((direction == "CE" and pcr >= C.PCR_TREND_MIN) or
                                        (direction == "PE" and pcr <= -C.PCR_TREND_MIN)):
                    score += 1
                    reasons.append(f"PCR trend {pcr:+.2f} agrees")
                risk = (s.sl - spot) * (1 if direction == "PE" else -1)

                against = ((trend_now == "down" and direction == "CE") or
                           (trend_now == "up" and direction == "PE"))
                if T.BLOCK_COUNTER_TREND and against:
                    decision = f"SKIP: against the {trend_now}trend"
                elif status == "exiting":
                    decision = f"SKIP: writers leaving {L:.0f} ({ch:+.1f}% OI) - level may break"
                elif risk <= 0:
                    decision = "SKIP: price already beyond stop"
                elif risk < C.MIN_RISK_POINTS:
                    decision = f"SKIP: stop too tight ({risk:.0f} pts)"
                elif risk > C.MAX_RISK_POINTS:
                    decision = f"SKIP: stop too far ({risk:.0f} pts)"
                elif score < C.MIN_SCORE:
                    decision = f"SKIP: score {score} < {C.MIN_SCORE}"
                elif day.by_dir[("reversal", direction)] >= C.MAX_TRADES_PER_DIRECTION:
                    decision = f"SKIP: already traded a reversal {direction} today"
                else:
                    decision = "CANDIDATE"
                    consider(dict(direction=direction, setup=s.name, level=L, score=score,
                                  sl=s.sl, risk=risk, reasons="; ".join(reasons), strategy="reversal",
                                  target_r=C.TARGET_R, trail_r=0.0))
                record(direction, s.name, L, score, reasons, decision)
    return best


def run(kite, clock, out_dir="logs", data_dir="data", tg=None):
    tg = tg or alerts.Telegram(None, None, enabled=False)
    today = clock.now().date()
    setup_logging(out_dir, today, clock)
    os.makedirs(data_dir, exist_ok=True)
    m = Market(kite, clock)
    expiry, chain, lot_size = m.load_options(today)
    prev = m.prev_day(today)
    tracker = ChainTracker(m, chain, f"{data_dir}/chain_{today}.csv")
    book = PaperBook(f"{out_dir}/trades.csv", log)
    signals_file = f"{out_dir}/signals.csv"
    day = Day()
    log.info("PAPER TRADING | %s | expiry %s | lot size %d | lots %d", C.UNDERLYING, expiry, lot_size, C.LOTS)
    log.info("Telegram alerts: %s", "ON" if tg.enabled else "off")
    if prev:
        log.info("Previous day: high %.0f  low %.0f  close %.0f", prev["high"], prev["low"], prev["close"])
    tg.send(alerts.start_msg(expiry, lot_size))
    st_hist = m.history(today, STM.ST_WARMUP_DAYS) if STM.ST_ENABLED else []
    log.info("Supertrend(%s,%s) paper strategy: %s (%d warm-up candles)", STM.ST_PERIOD, STM.ST_MULT,
             "ON" if STM.ST_ENABLED else "off", len(st_hist))

    at = lambda s: datetime.combine(today, hhmm(s), IST)
    open_t, start_t, last_entry_t, exit_t = (at(C.MARKET_OPEN), at(C.NO_TRADE_BEFORE),
                                              at(C.NO_NEW_TRADE_AFTER), at(C.EXIT_TIME))
    st_start_t, st_last_t = at(STM.ST_START), at(STM.ST_LAST_ENTRY)
    st = {"trade": None, "pnl": 0.0, "n": 0,     # Supertrend has its own position slot
          "last_dir": None, "first_done": False, "best": 0.0, "sl": None}
    while clock.now() < open_t:
        clock.sleep(min(30, (open_t - clock.now()).total_seconds()))

    trade = None
    last_chain = last_candle_check = None
    last_candle_time = None
    gap_checked = False
    levels_sent = False
    candles = []

    def opt_key(t):
        return f"NFO:{t.symbol}"

    def closed(t):
        net = book.record(t)
        day.pnl += net
        day.losses += net <= 0
        before = day.blocked
        day.check()
        tg.send(alerts.exit_msg(t, net))
        if day.blocked and day.blocked != before and "loss" in day.blocked:
            tg.send(f"⛔ No more trades today: {day.blocked}.")

    def st_close(reason):
        t = st["trade"]
        q = m.quote([C.SPOT_SYMBOL, opt_key(t)])
        bid, ask, ltp = best_prices(q.get(opt_key(t), {}))
        book.close(t, clock.now(), q[C.SPOT_SYMBOL]["last_price"], bid, ltp, reason)
        net = book.record(t)
        st["pnl"] += net
        st["trade"] = None
        tg.send(alerts.exit_msg(t, net))

    def st_open(direction, line, now):
        q0 = m.quote([C.SPOT_SYMBOL])
        spot_now = q0[C.SPOT_SYMBOL]["last_price"]
        atm = round(spot_now / C.STRIKE_STEP) * C.STRIKE_STEP
        inst = chain.get((float(atm), direction))
        if not inst:
            log.warning("Supertrend: no %s option at %s", direction, atm)
            return
        key = f"NFO:{inst['tradingsymbol']}"
        q = m.quote([key])
        bid, ask, ltp = best_prices(q[key])
        sgn = 1 if direction == "CE" else -1
        tgt = spot_now + sgn * STM.ST_TARGET_POINTS if STM.ST_TARGET_POINTS else 0.0
        t = book.open(direction=direction, symbol=inst["tradingsymbol"], lot_size=lot_size, lots=C.LOTS,
                      entry_time=now, entry_spot=spot_now, entry_premium=buy_price(ask, ltp),
                      sl_spot=line, target_spot=tgt, setup="supertrend", level=round(line, 1), score=0,
                      reasons=f"Supertrend({STM.ST_PERIOD},{STM.ST_MULT}) turned "
                              f"{'GREEN' if direction == 'CE' else 'RED'}; line {line:.1f}")
        st["trade"] = t
        st["n"] += 1
        st["best"], st["trail_on"] = 0.0, False
        st["sl"] = spot_now - sgn * STM.ST_STOP_POINTS if STM.ST_STOP_POINTS else None
        tg.send(alerts.st_entry_msg(t, line))

    try:
        while True:
            now = clock.now()
            keys = [C.SPOT_SYMBOL] + ([opt_key(trade)] if trade else [])
            try:
                q = m.quote(keys)
            except Exception as e:
                if "token" in str(e).lower():
                    raise
                log.warning("Quote failed (%s) - retrying", e)
                clock.sleep(C.LOOP_SECONDS)
                continue
            spot = q[C.SPOT_SYMBOL]["last_price"]

            # ---- manage open paper trade ----
            if trade:
                bid, ask, ltp = best_prices(q.get(opt_key(trade), {}))
                stop_now = now >= exit_t or os.path.exists("STOP")
                n_exits = len(trade.exits)
                done = book.manage(trade, now, spot, bid, ltp)
                if not done and len(trade.exits) > n_exits:
                    tg.send(alerts.partial_msg(trade, trade.exits[-1]))
                if not done and stop_now:
                    book.close(trade, now, spot, bid, ltp,
                               "time exit" if now >= exit_t else "manual STOP")
                    done = True
                if done:
                    closed(trade)
                    trade = None

            # ---- Supertrend target / trailing stop (checked every loop) ----
            stt = st["trade"]
            if stt:
                sgn = 1 if stt.direction == "CE" else -1
                st["best"] = max(st["best"], sgn * (spot - stt.entry_spot))
                if (STM.ST_TRAIL_MODE == "tick" and STM.ST_TRAIL_POINTS
                        and st["best"] >= STM.ST_TRAIL_START):
                    new_sl = stt.entry_spot + sgn * (st["best"] - STM.ST_TRAIL_POINTS)
                    if st["sl"] is None or sgn * (new_sl - st["sl"]) > 0:
                        st["sl"] = new_sl
                if stt.target_spot and sgn * (spot - stt.target_spot) >= 0:
                    st_close(f"target +{STM.ST_TARGET_POINTS} pts")
                elif st["sl"] is not None and sgn * (spot - st["sl"]) <= 0:
                    locked = sgn * (st["sl"] - stt.entry_spot)
                    st_close(f"trailing stop ({locked:+.0f} pts locked)" if locked > -STM.ST_STOP_POINTS
                             else f"stop-loss (-{STM.ST_STOP_POINTS} pts)")

            if now >= exit_t or os.path.exists("STOP"):
                if st["trade"]:
                    st_close("time exit" if now >= exit_t else "manual STOP")
                log.info("Stopping: %s", "exit time" if now >= exit_t else "STOP file found")
                break

            # ---- option chain (every minute) ----
            if last_chain is None or (now - last_chain).total_seconds() >= C.CHAIN_REFRESH_SECONDS:
                try:
                    tracker.refresh(spot)
                    res, sup = tracker.levels(spot)
                    log.info("NIFTY %.1f | resistance (Call OI) %s | support (Put OI) %s",
                             spot, [int(x) for x in res], [int(x) for x in sup])
                    if not levels_sent and now >= start_t:
                        levels_sent = True
                        tg.send(alerts.levels_msg(now, spot, res, sup))
                except Exception as e:
                    log.warning("Option chain refresh failed: %s", e)
                last_chain = now

            # ---- new candle -> look for setups ----
            can_trade = trade is None and day.blocked is None and start_t <= now <= last_entry_t
            if (last_candle_check is None or
                    (now - last_candle_check).total_seconds() >= C.CANDLE_POLL_SECONDS):
                last_candle_check = now
                try:
                    candles = m.candles(today)
                except Exception as e:
                    log.warning("Candle fetch failed: %s", e)
                if candles and not gap_checked and prev:
                    gap_checked = True
                    gap = abs(candles[0]["open"] - prev["close"]) / prev["close"] * 100
                    if gap > C.GAP_SKIP_PCT:
                        day.blocked = f"gap of {gap:.1f}% - possible trend day"
                        log.info("No trades today: %s (still recording data)", day.blocked)
                        tg.send(f"⏸ No trades today: {day.blocked}. Still recording data.")
                if candles and candles[-1]["time"] != last_candle_time:
                    last_candle_time = candles[-1]["time"]
                    if can_trade:
                        sig = evaluate(now, candles, spot, tracker, prev, day, signals_file, tg)
                        if sig:
                            trade = enter(m, book, chain, lot_size, sig, spot, now, day)
                            if trade:
                                tg.send(alerts.entry_msg(trade))
                    # ---- Supertrend: own slot, flips only at 5-min closes ----
                    if STM.ST_ENABLED:
                        d_now, line = STM.latest(st_hist + candles)
                        prev_dir, st["last_dir"] = st["last_dir"], d_now
                        fresh = prev_dir is not None and d_now != prev_dir
                        first = STM.ST_FIRST_TRADE and not st["first_done"] and now >= st_start_t
                        if now >= st_start_t:
                            st["first_done"] = True
                        held = None
                        if st["trade"]:
                            held = 1 if st["trade"].direction == "CE" else -1
                            # move the trailing stop at each 5-min close
                            if STM.ST_TRAIL_POINTS and st["best"] >= STM.ST_TRAIL_START:
                                new_sl = st["trade"].entry_spot + held * (st["best"] - STM.ST_TRAIL_POINTS)
                                if st["sl"] is None or held * (new_sl - st["sl"]) > 0:
                                    if not st.get("trail_on"):
                                        st["trail_on"] = True
                                        tg.send(f"🔒 Supertrend trade +{st['best']:.0f} pts: trailing stop ON "
                                                f"at NIFTY {new_sl:,.1f} ({held * (new_sl - st['trade'].entry_spot):+.0f} pts)")
                                    st["sl"] = new_sl
                                    log.info("Supertrend trailing stop -> %.1f (best +%.0f)", new_sl, st["best"])
                        if st["trade"] and d_now is not None and d_now != held:
                            st_close("Supertrend flip")
                        if (st["trade"] is None and d_now is not None and (fresh or first)
                                and st_start_t <= now <= st_last_t):
                            st_open("CE" if d_now == 1 else "PE", line, now)

            clock.sleep(C.LOOP_SECONDS)

    except KeyboardInterrupt:
        log.info("Ctrl+C pressed")
        if trade:
            q = m.quote([C.SPOT_SYMBOL, opt_key(trade)])
            bid, ask, ltp = best_prices(q[opt_key(trade)])
            book.close(trade, clock.now(), q[C.SPOT_SYMBOL]["last_price"], bid, ltp, "manual Ctrl+C")
            closed(trade)
        if st["trade"]:
            st_close("manual Ctrl+C")

    # save today's candles for future backtesting
    try:
        candles = m.candles(today)
        with open(f"{data_dir}/nifty_{C.CANDLE_MINUTES}min_{today}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time", "open", "high", "low", "close"])
            for c in candles:
                w.writerow([c["time"].strftime("%H:%M"), c["open"], c["high"], c["low"], c["close"]])
    except Exception as e:
        log.warning("Could not save candles: %s", e)
    log.info("Day finished. Paper P&L today: Rs %.0f (price-action Rs %.0f, Supertrend Rs %.0f)",
             day.pnl + st["pnl"], day.pnl, st["pnl"])
    tg.send(alerts.summary_msg(today, day.trades + st["n"], day.pnl + st["pnl"])
            + f"\nSupertrend: {st['n']} trades, Rs {st['pnl']:+,.0f}")
    tg.flush()
    report(f"{out_dir}/trades.csv", today.isoformat())


def enter(m, book, chain, lot_size, sig, spot, now, day):
    atm = round(spot / C.STRIKE_STEP) * C.STRIKE_STEP
    inst = chain.get((float(atm), sig["direction"]))
    if not inst:
        log.warning("No %s option at strike %s", sig["direction"], atm)
        return None
    key = f"NFO:{inst['tradingsymbol']}"
    q = m.quote([key, C.SPOT_SYMBOL])
    bid, ask, ltp = best_prices(q[key])
    spot = q[C.SPOT_SYMBOL]["last_price"]
    risk = (sig["sl"] - spot) * (1 if sig["direction"] == "PE" else -1)
    if risk <= 0:
        log.info("Price moved beyond stop before entry - skipped")
        return None
    day.register(sig)
    return book.open(**trade_args(sig, inst["tradingsymbol"], lot_size, now, spot, risk,
                                  buy_price(ask, ltp)))


def trade_args(sig, symbol, lot_size, now, spot, risk, premium):
    """Everything paper.open() needs - shared by bot.py and replay.py."""
    s = 1 if sig["direction"] == "CE" else -1
    return dict(direction=sig["direction"], symbol=symbol, lot_size=lot_size, lots=C.LOTS,
                entry_time=now, entry_spot=spot, entry_premium=premium, sl_spot=sig["sl"],
                target_spot=spot + s * risk * sig.get("target_r", C.TARGET_R),
                setup=sig["setup"], level=sig["level"], score=sig["score"],
                reasons=sig["reasons"], trail_r=sig.get("trail_r", 0.0))


def main():
    tg = alerts.Telegram.from_config()
    try:
        run(make_kite(), Clock(), tg=tg)
    except Exception as e:
        tg.send(f"⚠️ Bot STOPPED with an error:\n{e}\nCheck the black window / logs.")
        tg.flush()
        raise


if __name__ == "__main__":
    main()
