"""
Order-flow recorder (separate from the bot - does NOT trade, does NOT change bot logic).

Records every live tick of the current-month NIFTY FUTURES contract (the index itself
has no volume) with market depth, so that later we can test order-flow / footprint
scalping ideas on real data.

For each tick it saves:
  time, price, traded volume since last tick, who was aggressive
  (BUY = traded at/above the best ask, SELL = at/below the best bid),
  running delta (buy volume - sell volume) for the day,
  best bid / ask, total buy / sell quantity waiting in the order book, OI.

Run it every trading day AFTER  python login.py,  in a second command window
(the bot keeps running in the first one):

    python tick_recorder.py

Self tests:
    python tick_recorder.py --test             fake ticks, no Zerodha/internet (writes test_output/)
    python tick_recorder.py --test --telegram  same, and also sends the alerts to Telegram ([TEST])
    python tick_recorder.py --check            real Zerodha + Telegram connection check (after login.py)

It stops by itself at 15:31 (or press Ctrl+C). Output:
  data/ticks_YYYY-MM-DD.csv      every tick
  data/footprint_YYYY-MM-DD.csv  footprint: sell / buy quantity at each price, per 1-min candle
  data/predictions_YYYY-MM-DD.csv  next-candle guesses, RIGHT / WRONG
  data/signals_YYYY-MM-DD.csv      BUY CE / BUY PE signals with entry, stop, target, result
On screen AND on Telegram, after every 1-min candle: OHLC, sell, buy, delta, cumulative delta
and its trend, key level nearby, next-candle guesses - plus a separate 🚨 alert for each
BUY CE / BUY PE signal and its result. All logic is in orderflow.py.
(FOOTPRINT_TELEGRAM = False in config.py turns Telegram off.)
After 3-4 weeks of recordings we can backtest footprint / delta strategies.
"""
import csv
import json
import os
import sys
from datetime import datetime, date
from zoneinfo import ZoneInfo

from kiteconnect import KiteConnect, KiteTicker

import alerts
import config as C
import orderflow

IST = ZoneInfo("Asia/Kolkata")
STOP_AT = "15:31"
SPOT_TOKEN = 256265          # NSE:NIFTY 50 index (for spot price / futures premium)
# One tick bigger than this is a block / calendar-spread (rollover) trade or a reconnect jump,
# not normal aggressive buying/selling - it is saved but NOT counted in delta (26 Oct: 514,475 qty)
BLOCK_QTY = getattr(C, "OF_BLOCK_QTY", 100000)
# Send each 1-min candle's buy/sell/delta to Telegram (set FOOTPRINT_TELEGRAM = False in config.py to stop)
FOOTPRINT_TELEGRAM = getattr(C, "FOOTPRINT_TELEGRAM", True)


def _k(x):
    return f"{x / 1000:.2f}K" if abs(x) >= 1000 else f"{x:,}"


def load_token():
    with open("access_token.json") as f:
        tok = json.load(f)
    if tok.get("date") != date.today().isoformat():
        sys.exit("access_token.json is not from today - run  python login.py  first.")
    return tok["access_token"]


def nifty_future(kite):
    futs = [i for i in kite.instruments("NFO")
            if i["name"] == "NIFTY" and i["instrument_type"] == "FUT" and i["expiry"] >= date.today()]
    f = min(futs, key=lambda i: i["expiry"])
    return f["instrument_token"], f["tradingsymbol"]


def prev_day_hl(kite, fut_token):
    """Previous trading day's high / low of the futures contract (key levels)."""
    try:
        from datetime import timedelta
        rows = kite.historical_data(fut_token, date.today() - timedelta(days=10), date.today(), "day")
        rows = [r for r in rows if r["date"].date() < date.today()] if rows else []
        if rows:
            r = rows[-1]
            return [(r["high"], "Prev-day HIGH"), (r["low"], "Prev-day LOW")]
    except Exception as e:
        print(f"(could not load previous day high/low: {e})")
    return []


def day_pivots(kite):
    """Classic pivots (spot) from the previous day's NIFTY 50 high / low / close."""
    try:
        from datetime import timedelta
        rows = kite.historical_data(SPOT_TOKEN, date.today() - timedelta(days=10), date.today(), "day")
        rows = [r for r in rows if r["date"].date() < date.today()] if rows else []
        if rows:
            r = rows[-1]
            H, L, Cl = r["high"], r["low"], r["close"]
            P = (H + L + Cl) / 3
            return {"R2": P + H - L, "R1": 2 * P - L, "P": P, "S1": 2 * P - H, "S2": P - H + L}, (H, L, Cl)
    except Exception as e:
        print(f"(could not load pivots: {e})")
    return {}, None


def live_premium(kite, fut_symbol):
    try:
        q = kite.quote(["NSE:NIFTY 50", f"NFO:{fut_symbol}"])
        return q[f"NFO:{fut_symbol}"]["last_price"] - q["NSE:NIFTY 50"]["last_price"]
    except Exception:
        return None


def map_text(piv, static, premium):
    """Morning message: all key levels, spot and futures, sorted top to bottom."""
    rows = [(v, f"Pivot {k}", True) for k, v in piv.items()]
    rows += [(p - (premium or 0), n, False) for p, n in static]
    rows.sort(key=lambda r: -r[0])
    out = ["🗺 TODAY'S LEVELS (spot | futures)" + (f"  premium ≈ {premium:+.0f}" if premium is not None else "")]
    for spot_v, name, _ in rows:
        fut = f"{spot_v + premium:,.0f}" if premium is not None else "-"
        mark = "  ← dividing line (above = CE side, below = PE side)" if name == "Pivot P" else ""
        out.append(f"{spot_v:,.0f} | {fut}  {name}{mark}")
    return "\n".join(out)


def replay_today(path, flow, state):
    """After a restart: replay today's saved ticks so cumulative delta, levels, guesses and
    signal limits continue from where they were (nothing is re-sent to Telegram)."""
    candles = {}
    order = []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            m = (r.get("time") or "")[:5]
            if m < "09:15" or not r.get("price"):
                continue
            px = float(r["price"])
            if m not in candles:
                candles[m] = {"o": px, "h": px, "l": px, "c": px, "fp": {}}
                order.append(m)
            c = candles[m]
            c["h"], c["l"], c["c"] = max(c["h"], px), min(c["l"], px), px
            q = int(float(r.get("qty") or 0))
            if q and q <= BLOCK_QTY and r.get("side") in ("BUY", "SELL"):
                lvl = c["fp"].setdefault(px, [0, 0])
                lvl[1 if r["side"] == "BUY" else 0] += q
    if not order:
        return
    cum = 0
    for m in order[:-1]:                       # all finished minutes
        c = candles[m]
        sell = sum(v[0] for v in c["fp"].values())
        buy = sum(v[1] for v in c["fp"].values())
        cum += buy - sell
        flow.on_candle(m, c["o"], c["h"], c["l"], c["c"], buy, sell, c["fp"], cum)
    last = candles[order[-1]]                  # the minute we were in: continue it live
    cum += sum(v[1] - v[0] for v in last["fp"].values())
    state.update({"min": order[-1], "fp": last["fp"], "o": last["o"], "h": last["h"],
                  "l": last["l"], "c": last["c"], "delta": cum})
    print(f"Restarted: replayed {len(order) - 1} candles from today's file "
          f"(cum delta {cum:+,}, signals so far {len(flow.signals)})")


def main():
    token = load_token()
    kite = KiteConnect(api_key=C.API_KEY)
    kite.set_access_token(token)
    fut_token, fut_symbol = nifty_future(kite)

    os.makedirs("data", exist_ok=True)
    path = f"data/ticks_{datetime.now(IST):%Y-%m-%d}.csv"
    new = not os.path.exists(path)
    out = open(path, "a", newline="", encoding="utf-8")
    w = csv.writer(out)
    if new:
        w.writerow(["time", "price", "qty", "side", "cum_delta", "bid", "ask",
                    "bid_qty", "ask_qty", "total_buy_qty", "total_sell_qty", "volume", "oi"])

    state = {"vol": None, "bid": None, "ask": None, "delta": 0, "n": 0, "price": None,
             "min": None, "fp": {}, "o": None, "h": None, "l": None, "c": None}
    # footprint: one row per (1-min candle, price level) with aggressive sell / buy quantity
    fp_path = f"data/footprint_{datetime.now(IST):%Y-%m-%d}.csv"
    fp_new = not os.path.exists(fp_path)
    fp_out = open(fp_path, "a", newline="", encoding="utf-8")
    fw = csv.writer(fp_out)
    if fp_new:
        fw.writerow(["candle", "price", "sell_qty", "buy_qty", "level_delta"])
    today = datetime.now(IST).strftime("%Y-%m-%d")
    pr_path = f"data/predictions_{today}.csv"
    pr_new = not os.path.exists(pr_path)
    pr_out = open(pr_path, "a", newline="", encoding="utf-8")
    pw = csv.writer(pr_out)
    if pr_new:
        pw.writerow(["candle", "pattern", "predicted", "close", "next_candle", "next_close", "move", "result"])
    sg_path = f"data/signals_{today}.csv"
    sg_new = not os.path.exists(sg_path)
    sg_out = open(sg_path, "a", newline="", encoding="utf-8")
    sw = csv.writer(sg_out)
    if sg_new:
        sw.writerow(["time", "side", "kind", "level_name", "level", "why", "entry", "stop", "target",
                     "exit_time", "exit", "fut_points", "est_rs_per_lot", "result", "held_min"])
    static = prev_day_hl(kite, fut_token) + orderflow.prev_day_levels("data", today)
    flow = orderflow.Flow(fut_symbol, static, "data", today)
    piv, _hlc = day_pivots(kite)
    flow.pivots = piv                       # display only (not used for signals)
    premium = live_premium(kite, fut_symbol)
    morning_map = map_text(piv, static, premium)
    print(morning_map)

    def log_guess(row):
        pw.writerow(row)
        pr_out.flush()

    def log_signal(x):
        sw.writerow([x["t"], x["side"], x["kind"], x["lname"], round(x["level"], 1), x["why"], x["entry"],
                     round(x["stop"], 1), round(x["target"], 1), x["exit_t"], x["exit"], round(x["pts"], 1),
                     round(x["rs"]), x["result"], x["held"]])
        sg_out.flush()

    # restarted during the day?  rebuild today's candles from the ticks already saved
    if not new:
        out.flush()
        replay_today(path, flow, state)
    flow.on_guess, flow.on_signal = log_guess, log_signal
    if static:
        print("Key levels from yesterday: " + ", ".join(f"{n} {p:,.1f}" for p, n in static))
    print(f"Footprint per 1-min candle -> {fp_path}")
    print(f"{'candle':<7}{'open':>10}{'high':>10}{'low':>10}{'close':>10}"
          f"{'total sell':>12}{'total buy':>11}{'delta':>9}{'cum delta':>11}")

    def close_candle():
        if state["min"] is None:
            return
        sell = sum(v[0] for v in state["fp"].values())
        buy = sum(v[1] for v in state["fp"].values())
        for px in sorted(state["fp"], reverse=True):
            sq, bq = state["fp"][px]
            fw.writerow([state["min"], px, sq, bq, bq - sq])
        fp_out.flush()
        print(f"{state['min']:<7}{state['o']:>10.1f}{state['h']:>10.1f}{state['l']:>10.1f}{state['c']:>10.1f}"
              f"{sell:>12,}{buy:>11,}{buy - sell:>+9,}{state['delta']:>+11,}")
        text, sigs = flow.on_candle(state["min"], state["o"], state["h"], state["l"], state["c"],
                                    buy, sell, state["fp"], state["delta"])
        for line in text.split("\n")[4:]:
            print("        " + line)
        for m in sigs:
            print("\n" + m + "\n")
        if tg and tg.enabled:
            tg.send(text)
            for m in sigs:
                tg.send(m)
    print(f"Recording {fut_symbol} ticks to {path}  (stops at {STOP_AT}, Ctrl+C to stop)")

    tg = alerts.Telegram.from_config() if FOOTPRINT_TELEGRAM else None
    if tg and tg.enabled:
        tg.send(f"🎙 Order-flow recorder started ({fut_symbol}) - one message per 1-min candle")
        tg.send(morning_map)

    kws = KiteTicker(C.API_KEY, token)

    def on_ticks(ws, ticks):
        now = datetime.now(IST)
        for t in ticks:
            if t.get("instrument_token") == SPOT_TOKEN:
                flow.spot = t.get("last_price") or flow.spot      # live NIFTY 50 index
                continue
            if t.get("instrument_token") != fut_token:
                continue
            vol = t.get("volume_traded") or 0
            qty = 0 if state["vol"] is None else max(0, vol - state["vol"])
            state["vol"] = vol
            price = t.get("last_price")
            state["price"] = price
            side = ""
            minute = now.strftime("%H:%M")
            if minute < "09:15":            # pre-open session: not real trading, ignore
                state["vol"] = vol
                continue
            if minute != state["min"]:
                safe_close()
                state.update({"min": minute, "fp": {}, "o": price, "h": price, "l": price})
            state["h"], state["l"], state["c"] = max(state["h"], price), min(state["l"], price), price
            if qty > BLOCK_QTY:
                print(f"   (ignored block/spread trade {qty:,} qty at {price} - not counted in delta)")
                side = "BLOCK"
            elif qty:
                # compare with the order book just BEFORE this trade
                if state["ask"] is not None and price >= state["ask"]:
                    side, state["delta"] = "BUY", state["delta"] + qty
                elif state["bid"] is not None and price <= state["bid"]:
                    side, state["delta"] = "SELL", state["delta"] - qty
                else:
                    side = "MID"
                lvl = state["fp"].setdefault(price, [0, 0])
                if side == "BUY":
                    lvl[1] += qty
                elif side == "SELL":
                    lvl[0] += qty
            depth = t.get("depth") or {}
            b = (depth.get("buy") or [{}])[0]
            a = (depth.get("sell") or [{}])[0]
            state["bid"], state["ask"] = b.get("price") or state["bid"], a.get("price") or state["ask"]
            w.writerow([now.strftime("%H:%M:%S.%f")[:-3], price, qty, side, state["delta"],
                        b.get("price"), a.get("price"), b.get("quantity"), a.get("quantity"),
                        t.get("total_buy_quantity"), t.get("total_sell_quantity"), vol, t.get("oi")])
            state["n"] += 1
        if state["n"] % 500 < len(ticks):
            out.flush()
        if now.strftime("%H:%M") >= STOP_AT and not state.get("stopped"):
            state["stopped"] = True
            safe_close()
            print("Market closed - stopping.")
            out.flush()
            ws.close()
            ws.stop()

    def safe_close():
        try:
            close_candle()
        except Exception as e:             # never let one bad candle kill the recording
            import traceback
            traceback.print_exc()
            print(f"!! candle {state['min']} analysis error (recording continues): {e}")

    def on_connect(ws, response):
        ws.subscribe([fut_token, SPOT_TOKEN])
        ws.set_mode(ws.MODE_FULL, [fut_token])
        ws.set_mode(ws.MODE_LTP, [SPOT_TOKEN])

    def on_close(ws, code, reason):
        out.flush()

    kws.on_ticks = on_ticks
    kws.on_connect = on_connect
    kws.on_close = on_close
    try:
        kws.connect()          # blocks until closed
    except KeyboardInterrupt:
        pass
    finally:
        if not state.get("stopped"):
            safe_close()
        out.flush()
        out.close()
        fp_out.close()
        pr_out.close()
        if state["c"] is not None:
            fin = flow.close_open_signal(state["min"] or "15:15", state["c"])
            if fin and tg and tg.enabled:
                tg.send(fin)
        sg_out.close()
        summ = flow.day_summary()
        print("\nToday: " + "\n       ".join(summ))
        if tg and tg.enabled and (flow.score or flow.signals):
            tg.send("📋 Order-flow day summary\n" + "\n".join(summ)
                    + "\n(guesses: 50% = coin toss | files: " + pr_path + ", " + sg_path + ")")
        if tg:
            tg.flush()
        print(f"Saved {state['n']:,} ticks to {path}")


# ---------------------------------------------------------------- self tests
def _fake_run(minutes=40, telegram=False):
    """python tick_recorder.py --test : made-up ticks, no Zerodha, no internet.
    Writes into test_output/ (your real data/ folder is not touched)."""
    import random
    import shutil
    from datetime import timedelta
    global datetime, KiteConnect, KiteTicker, STOP_AT
    real_dt = datetime
    base = real_dt(2026, 10, 22, 9, 15, tzinfo=IST)
    clock = [base]

    class FakeDT(real_dt):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    class FakeKite:
        def __init__(self, **k):
            pass

        def set_access_token(self, t):
            pass

        def instruments(self, ex):
            return [{"name": "NIFTY", "instrument_type": "FUT", "expiry": date.today() + timedelta(days=5),
                     "instrument_token": 9, "tradingsymbol": "NIFTYTESTFUT"}]

        def historical_data(self, *a):
            return [{"date": real_dt(2026, 10, 21), "high": 22720.0, "low": 22640.0, "close": 22690.0}]

        def quote(self, keys):
            return {k: {"last_price": 22700.0 if "FUT" in k else 22650.0} for k in keys}

    class FakeTicker:
        MODE_FULL = "full"
        MODE_LTP = "ltp"

        def __init__(self, *a):
            pass

        def subscribe(self, x):
            pass

        def set_mode(self, m, x):
            pass

        def close(self):
            pass

        def stop(self):
            pass

        def connect(self):
            self.on_connect(self, None)
            v, p = 1000, 22700.0
            rnd = random.Random(3)
            for k in range(minutes * 30):
                clock[0] = base + timedelta(seconds=k * 2)
                m = k // 30
                bias = -0.6 if 8 <= m <= 12 else 0.6 if 20 <= m <= 24 else 0
                p = round((p + rnd.choice([-0.5, 0, 0.5]) + bias) * 2) / 2
                v += rnd.choice([65, 130, 0]) + (650 if bias else 0)
                self.on_ticks(self, [{"instrument_token": 9, "last_price": p, "volume_traded": v, "oi": 1,
                                      "depth": {"buy": [{"price": p - 0.5, "quantity": 650}],
                                                "sell": [{"price": p + 0.5, "quantity": 520}]}}])

    shutil.rmtree("test_output", ignore_errors=True)
    os.makedirs("test_output", exist_ok=True)
    home = os.getcwd()
    with open("test_output/access_token.json", "w") as f:
        json.dump({"date": date.today().isoformat(), "access_token": "test"}, f)
    datetime, KiteConnect, KiteTicker, STOP_AT = FakeDT, FakeKite, FakeTicker, "23:59"
    if not telegram:
        alerts.Telegram.from_config = classmethod(lambda cls, prefix="": alerts.Telegram("", "", False))
    else:
        orig = alerts.Telegram.from_config.__func__
        alerts.Telegram.from_config = classmethod(lambda cls, prefix="": orig(cls, "[TEST] "))
    os.chdir("test_output")
    try:
        print(f"*** TEST MODE: {minutes} minutes of MADE-UP ticks (not real market data) ***\n")
        main()
    finally:
        os.chdir(home)
    print("\nTEST FINISHED WITHOUT ERRORS. Files written:")
    for f in sorted(os.listdir("test_output/data")):
        print("   test_output/data/" + f)


def _check():
    """python tick_recorder.py --check : real Zerodha connection test (after login.py), no recording."""
    token = load_token()
    kite = KiteConnect(api_key=C.API_KEY)
    kite.set_access_token(token)
    fut_token, fut_symbol = nifty_future(kite)
    print(f"OK  login token works, futures contract: {fut_symbol} (token {fut_token})")
    q = kite.quote([f"NFO:{fut_symbol}"])[f"NFO:{fut_symbol}"]
    print(f"OK  live quote: last price {q.get('last_price')}  volume {q.get('volume')}  "
          f"buy qty {q.get('buy_quantity')}  sell qty {q.get('sell_quantity')}")
    lv = prev_day_hl(kite, fut_token)
    print("OK  previous day levels: " + (", ".join(f"{n} {p:,.1f}" for p, n in lv) or "none found"))
    lv2 = orderflow.prev_day_levels("data", datetime.now(IST).strftime("%Y-%m-%d"))
    print("OK  yesterday's footprint levels: " + (", ".join(f"{n} {p:,.1f}" for p, n in lv2)
                                                  or "none yet (needs one recorded day)"))
    tg = alerts.Telegram.from_config(prefix="[CHECK] ")
    if tg.enabled:
        tg.send("✅ tick_recorder check: Zerodha + Telegram OK")
        tg.flush()
        print("OK  Telegram test message sent - check your phone")
    else:
        print("--  Telegram is off / not set up")
    print("\nAll checks passed. Tomorrow just run:  python tick_recorder.py")


if __name__ == "__main__":
    if "--test" in sys.argv:
        _fake_run(telegram="--telegram" in sys.argv)
    elif "--check" in sys.argv:
        _check()
    else:
        main()
