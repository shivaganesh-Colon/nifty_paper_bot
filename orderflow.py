"""
Order-flow brain for tick_recorder.py (NIFTY FUTURES, 1-min candles).
Does NOT place trades - it sends alerts and keeps score, so we can learn what works.

Every closed 1-min candle gets:
  - sell / buy / delta / cumulative delta and its TREND (rising / falling / flat)
  - LOCATION: is price near a key level?
        previous day high/low, previous day POC / value area (from yesterday's recording),
        today's high/low, today's POC / value area, volume ANCHORS (prices where heavy
        buying/selling happened in a big candle), option OI walls (from the bot's chain file)
  - NEXT-CANDLE GUESSES (scored one candle later):
        STRENGTH     big delta on the same side 2 candles + price following  -> continue
        ABSORPTION   big |delta| but tiny candle (effort, no result)          -> reverse
        EXHAUSTION   delta shrinking 3 candles while price still pushing      -> reverse
        DIVERGENCE   candle colour vs delta disagree -> logged two ways:
                        DIV-FOLLOW (follow delta, video 2) and DIV-FADE (video 1)
  - SIGNALS (paper only, tracked to stop / target / 20-min timeout):
        REVERSAL  at a support (CE) / resistance (PE): level tested and held, order flow
                  turned (absorption, exhaustion or strong opposite delta) and the candle
                  closed beyond the previous candle (confirmation). Stop just beyond the level.
        BREAKOUT  candle closes through a level with big delta, cumulative delta agreeing
                  and candle colour agreeing (delta + cum delta + candle all aligned).
"""
import csv
import glob
import os

import config as C

BIN = getattr(C, "OF_BIN", 5)                 # price bucket (points) for volume profile / anchors
BIG_X = getattr(C, "OF_BIG_X", 1.5)           # big delta = 1.5 x average |delta| (last 20 candles)
MIN_CANDLES = 5
SIGNAL_START = getattr(C, "OF_SIGNAL_START", "09:25")
SIGNAL_END = getattr(C, "OF_SIGNAL_END", "15:00")
SIGNAL_TIMEOUT = getattr(C, "OF_SIGNAL_TIMEOUT", 45)   # minutes (trailing stop does the work)
TRAIL_START = getattr(C, "OF_TRAIL_START", 50)         # futures pts in profit before trailing starts
TRAIL_GAP = getattr(C, "OF_TRAIL_GAP", 40)             # trailing stop distance behind the best price
EXPLOSIVE_X = getattr(C, "OF_EXPLOSIVE_X", 5.0)        # delta >= 5x the average |delta|
SIDEWAYS_MAX = getattr(C, "OF_SIDEWAYS_MAX", 130)      # last-hour range (pts) at or below this = sideways
LONG_MOVE = getattr(C, "OF_LONG_MOVE", 150)            # move without a real pullback -> "be careful" warning
MAX_RISK = getattr(C, "OF_MAX_RISK", 40)               # skip signals needing a stop wider than this
LOT = getattr(C, "LOT_SIZE_FALLBACK", 65)
MAX_SIGNALS = getattr(C, "OF_MAX_SIGNALS", 20)         # per day - quality over quantity
COOLDOWN = getattr(C, "OF_COOLDOWN", 5)                # minutes after a signal ends
MIN_TARGET = getattr(C, "OF_MIN_TARGET", 20)           # futures points; smaller moves don't beat option costs


def _b(p):
    return round(p / BIN) * BIN


def _k(x):
    return f"{x / 1000:.2f}K" if abs(x) >= 1000 else f"{x:,}"


def value_area(profile, share=0.70):
    """profile: {bin_price: volume}. Returns (poc, val, vah) or (None,)*3."""
    if not profile:
        return None, None, None
    prices = sorted(profile)
    poc = max(prices, key=lambda p: profile[p])
    total = sum(profile.values())
    i = j = prices.index(poc)
    got = profile[poc]
    while got < share * total and (i > 0 or j < len(prices) - 1):
        down = profile[prices[i - 1]] if i > 0 else -1
        up = profile[prices[j + 1]] if j < len(prices) - 1 else -1
        if up >= down:
            j += 1
            got += profile[prices[j]]
        else:
            i -= 1
            got += profile[prices[i]]
    return poc, prices[i], prices[j]


def prev_day_levels(data_dir, today):
    """POC / VAL / VAH of the most recent earlier footprint file, if any."""
    files = sorted(f for f in glob.glob(os.path.join(data_dir, "footprint_*.csv"))
                   if os.path.basename(f) < f"footprint_{today}.csv")
    if not files:
        return []
    prof = {}
    with open(files[-1], encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            p = _b(float(r["price"]))
            prof[p] = prof.get(p, 0) + int(float(r["sell_qty"])) + int(float(r["buy_qty"]))
    poc, val, vah = value_area(prof)
    if poc is None:
        return []
    return [(poc, "Prev-day POC"), (val, "Prev-day VAL"), (vah, "Prev-day VAH")]


def oi_walls(data_dir, today, fut_price):
    """Max Call-OI / Put-OI strikes from the bot's chain file, converted to futures prices."""
    path = os.path.join(data_dir, f"chain_{today}.csv")
    if not os.path.exists(path) or fut_price is None:
        return [], None
    last = {}
    spot = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return [], None
        t = rows[-1]["time"]
        for r in rows:
            if r["time"] == t:
                last[float(r["strike"])] = (float(r["ce_oi"] or 0), float(r["pe_oi"] or 0))
                spot = float(r["spot"])
    except Exception:
        return [], None
    if not last or not spot:
        return [], None
    basis = fut_price - spot
    ce = max(last, key=lambda k: last[k][0])
    pe = max(last, key=lambda k: last[k][1])
    return [(ce + basis, f"Call-OI wall {ce:.0f}"), (pe + basis, f"Put-OI wall {pe:.0f}")], basis


class Flow:
    def __init__(self, symbol, static_levels=None, data_dir="data", today=""):
        self.symbol = symbol
        self.static = list(static_levels or [])     # [(price, name)]
        self.data_dir, self.today = data_dir, today
        self.hist = []          # closed candles
        self.profile = {}       # bin -> volume (today)
        self.anchors = []       # (price, name)
        self.pending = []       # guesses waiting for next candle: (t, name, dir, close)
        self.score = {}         # guess name -> [right, total]
        self.signal = None      # open paper signal
        self.signals = []       # finished signals
        self.used_levels = set()  # levels already traded today (no second try at the same level)
        self.last_exit = None
        self.basis = None
        self.oi = []
        self.on_guess = None    # callback(row) for each scored guess  (csv logging)
        self.on_signal = None   # callback(signal dict) when a signal finishes
        self.spot = None        # latest NIFTY spot (set by the recorder) -> premium
        self.pivots = {}        # spot pivots {name: level} - shown in alerts only, not used for signals
        self.last_explosive = {}  # side -> time of last explosive candle

    # ---------------------------------------------------------------- helpers
    def _avgs(self):
        last = self.hist[-21:-1] or self.hist[:-1] or self.hist
        n = len(last)
        avg_d = sum(abs(x["d"]) for x in last) / n or 1
        avg_v = sum(x["v"] for x in last) / n or 1
        avg_r = sum(x["h"] - x["l"] for x in last) / n or 1
        return avg_d, avg_v, avg_r

    def levels(self):
        lv = list(self.static) + list(self.oi) + list(self.anchors[-6:])
        if len(self.hist) >= 15:               # today's high/low only once the day has some shape
            past = self.hist[:-1]
            lv += [(max(x["h"] for x in past), "Today HIGH"), (min(x["l"] for x in past), "Today LOW")]
        poc, val, vah = value_area(self.profile)
        if poc is not None and len(self.hist) >= 15:
            lv += [(poc, "Today POC"), (val, "Today VAL"), (vah, "Today VAH")]
        return lv

    def sp(self, fut_price):
        """futures price -> approx. spot text"""
        if self.basis is None:
            return ""
        return f" (spot ≈ {fut_price - self.basis:,.0f})"

    def refresh_oi(self):
        if self.hist:
            self.oi, b = oi_walls(self.data_dir, self.today, self.hist[-1]["c"])
            if b is not None and self.spot is None:
                self.basis = b

    # ---------------------------------------------------------------- main
    def on_candle(self, t, o, h, l, c, buy, sell, fp, cum):
        """fp: {price: [sell, buy]}. Returns (candle_text, signal_texts)."""
        self._new = []
        out = self._on_candle(t, o, h, l, c, buy, sell, fp, cum)
        self.anchors += self._new          # a candle's own anchors count only from the next candle
        return out

    def _on_candle(self, t, o, h, l, c, buy, sell, fp, cum):
        d = buy - sell
        self.hist.append({"t": t, "o": o, "h": h, "l": l, "c": c, "d": d, "v": buy + sell, "cum": cum})
        lv_fp = {}
        for px, (sq, bq) in fp.items():
            bp = _b(px)
            self.profile[bp] = self.profile.get(bp, 0) + sq + bq
            s = lv_fp.setdefault(bp, [0, 0])
            s[0] += sq
            s[1] += bq
        if len(self.hist) % 5 == 1:
            self.refresh_oi()
        if self.spot:
            self.basis = c - self.spot
        avg_d, avg_v, avg_r = self._avgs()
        big = BIG_X * avg_d
        near = max(8.0, 0.5 * avg_r)
        lines, sig_msgs = [], []
        self._warn = []          # warnings copied into any signal sent this candle
        self._explosive = None

        # anchors: heavy-volume price inside a big candle
        if len(self.hist) >= MIN_CANDLES and buy + sell >= 2.0 * avg_v and lv_fp:
            ps = max(lv_fp, key=lambda p: lv_fp[p][0])
            pb = max(lv_fp, key=lambda p: lv_fp[p][1])
            if lv_fp[ps][0] >= 0.25 * (buy + sell):
                self._new.append((ps, f"Sell anchor {t}"))
            if lv_fp[pb][1] >= 0.25 * (buy + sell):
                self._new.append((pb, f"Buy anchor {t}"))

        # cumulative delta trend (last 5 candles)
        trend = "flat"
        if len(self.hist) >= 6:
            ch = cum - self.hist[-6]["cum"]
            trend = "RISING ↑" if ch > big else "FALLING ↓" if ch < -big else "flat"

        # 1) score last candle's guesses
        if self.pending:
            res = []
            for (pt, name, gdir, pc) in self.pending:
                move = c - pc
                ok = move > 0 if gdir == "UP" else move < 0
                sc = self.score.setdefault(name, [0, 0])
                sc[0] += ok
                sc[1] += 1
                res.append(f"{'✅' if ok else '❌'} {name} {gdir} ({move:+.1f})")
                if self.on_guess:
                    self.on_guess([pt, name, gdir, pc, t, c, round(move, 2), "RIGHT" if ok else "WRONG"])
            self.pending = []
            right = sum(v[0] for v in self.score.values())
            total = sum(v[1] for v in self.score.values())
            lines.append("Last guess: " + ", ".join(res) + f" | Today {right}/{total} ({100 * right / total:.0f}%)")

        # 2) track the open signal on this candle
        if self.signal:
            fin = self._track(t, h, l, c)
            if fin:
                sig_msgs.append(fin)

        # 3) location
        loc = []
        for p, name in self.levels():
            if abs(c - p) <= near or (l - near <= p <= h + near and (name.startswith("Prev") or "wall" in name)):
                loc.append((abs(c - p), p, name))
        loc.sort()
        if loc:
            p, name = loc[0][1], loc[0][2]
            lines.append(f"📍 Near {name} {p:,.1f}{self.sp(p)} ({'support below' if p <= c else 'resistance above'})")

        if len(self.hist) >= MIN_CANDLES:
            self._context(t, o, h, l, c, d, buy, sell, lv_fp, avg_d, avg_v, avg_r, lines)

        if len(self.hist) < MIN_CANDLES:
            return self._text(t, o, h, l, c, buy, sell, d, cum, trend, lines), sig_msgs

        a, b = self.hist[-2], self.hist[-1]
        col = 1 if c > o else -1 if c < o else 0

        # 4) next-candle guesses
        guesses = []
        move2 = b["c"] - a["o"]
        if a["d"] <= -big and b["d"] <= -big and move2 < 0:
            guesses.append(("STRENGTH", "DOWN", f"2 candles strong selling ({_k(a['d'])}, {_k(b['d'])})"))
        elif a["d"] >= big and b["d"] >= big and move2 > 0:
            guesses.append(("STRENGTH", "UP", f"2 candles strong buying ({_k(a['d'])}, {_k(b['d'])})"))
        if abs(d) >= big and (h - l) <= 0.6 * avg_r:
            guesses.append(("ABSORPTION", "DOWN" if d > 0 else "UP",
                            f"{'buyers' if d > 0 else 'sellers'} hit hard ({_k(d)}) but candle only {h - l:.1f} pts"))
        if len(self.hist) >= 4:
            x, y, z = self.hist[-3], self.hist[-2], self.hist[-1]
            if x["d"] > y["d"] > z["d"] > 0 and z["c"] >= x["c"] and x["d"] >= big:
                guesses.append(("EXHAUSTION", "DOWN", f"buying fading {_k(x['d'])}→{_k(y['d'])}→{_k(z['d'])}"))
            if x["d"] < y["d"] < z["d"] < 0 and z["c"] <= x["c"] and x["d"] <= -big:
                guesses.append(("EXHAUSTION", "UP", f"selling fading {_k(x['d'])}→{_k(y['d'])}→{_k(z['d'])}"))
        if col and d and (col > 0) != (d > 0) and abs(d) >= 0.5 * avg_d:
            fdir = "UP" if d > 0 else "DOWN"
            guesses.append(("DIV-FOLLOW", fdir, f"{'green' if col > 0 else 'red'} candle but delta {_k(d)}"))
            guesses.append(("DIV-FADE", "DOWN" if fdir == "UP" else "UP", "opposite view (video 1)"))
        for name, gdir, why in guesses:
            self.pending.append((t, name, gdir, c))
            arrow = "⬆️ UP" if gdir == "UP" else "⬇️ DOWN"
            lines.append(f"🔮 {name}: next candle {arrow} - {why}")

        # 5) signals
        cool = self.last_exit is None or _mins(t) - _mins(self.last_exit) >= COOLDOWN
        if (not self.signal and SIGNAL_START <= t <= SIGNAL_END and (cool or self._explosive)
                and len(self.signals) < MAX_SIGNALS):
            s = self._find_signal(t, o, h, l, c, d, trend, big, near, avg_r, guesses)
            if s:
                self.signal = s
                sig_msgs.append(self._sig_open_text(s))
        return self._text(t, o, h, l, c, buy, sell, d, cum, trend, lines), sig_msgs

    # ---------------------------------------------------------------- context alerts
    def _context(self, t, o, h, l, c, d, buy, sell, lv_fp, avg_d, avg_v, avg_r, lines):
        rng = h - l
        vol = buy + sell
        # A) BIG VOLUME, SMALL MOVE -> a big buyer/seller is sitting there (absorption)
        top = max(lv_fp.items(), key=lambda kv: kv[1][0] + kv[1][1])[0] if lv_fp else c
        w3 = self.hist[-3:]
        v3 = sum(x["v"] for x in w3)
        d3 = sum(x["d"] for x in w3)
        move3 = w3[-1]["c"] - w3[0]["o"]
        hit = None
        if vol >= 2 * avg_v and rng <= 0.6 * avg_r:
            hit = (f"volume {vol / avg_v:.1f}x normal but candle only {rng:.0f} pts", d, "1 min")
        elif len(w3) == 3 and v3 >= 2 * 3 * avg_v and abs(move3) <= 0.8 * avg_r:
            hit = (f"3-min volume {v3 / (3 * avg_v):.1f}x normal but price moved only {move3:+.0f} pts", d3, "3 min")
        elif vol >= avg_v and rng <= 0.6 * avg_r and min(buy, sell) and max(buy, sell) >= 4 * min(buy, sell):
            hit = (f"{'buyers' if d > 0 else 'sellers'} {max(buy, sell) / min(buy, sell):.1f}x stronger "
                   f"but candle only {rng:.0f} pts", d, "1 min")
        if hit:
            why, dd, span = hit
            if dd > 0:
                who = (f"BIG SELLER sitting near {top:,.1f}{self.sp(top)} - buyers hit hard but price "
                       f"can't go up (sellers absorbing). If price breaks DOWN = buyers trapped (PE). "
                       f"If it breaks UP with big delta = seller finished (CE)")
            elif dd < 0:
                who = (f"BIG BUYER sitting near {top:,.1f}{self.sp(top)} - sellers hit hard but price "
                       f"can't go down (buyers absorbing). If price breaks UP = sellers trapped (CE). "
                       f"If it breaks DOWN with big delta = buyer finished (PE)")
            else:
                who = f"big fight near {top:,.1f}{self.sp(top)} - both sides heavy, wait for the break"
            lines.append(f"🧱 BIG VOLUME, SMALL MOVE ({span}): {why}\n   {who}")

        # B) EXPLOSIVE candle = effort WITH result
        col = 1 if c > o else -1 if c < o else 0
        if abs(d) >= EXPLOSIVE_X * avg_d and rng >= 1.5 * avg_r and col * d > 0:
            side = "UP" if d > 0 else "DOWN"
            lines.append(f"🔥 EXPLOSIVE {'BUYING' if d > 0 else 'SELLING'}: delta {abs(d) / avg_d:.0f}x normal "
                         f"and candle {rng:.0f} pts ({rng / avg_r:.1f}x) - effort WITH result, move {side}")
            prev = self.last_explosive.get(side)
            if prev is None or _mins(t) - _mins(prev) >= 30:     # only the FIRST burst counts
                self._explosive = (1 if d > 0 else -1, (h + l) / 2, f"explosive {'buying' if d > 0 else 'selling'} "
                                   f"{_k(d)} ({abs(d) / avg_d:.0f}x normal), candle {rng:.0f} pts")
            self.last_explosive[side] = t

        # C) SIDEWAYS warning (last 60 min range small and price inside it)
        w = self.hist[-60:]
        if len(w) >= 45:
            hi = max(x["h"] for x in w)
            lo = min(x["l"] for x in w)
            span = hi - lo
            if span <= max(SIDEWAYS_MAX, 0) and span > 0:
                pos = (c - lo) / span
                where = ("price near the TOP = resistance" if pos >= 0.8 else
                         "price near the BOTTOM = support" if pos <= 0.2 else
                         "price in the MIDDLE - breakouts often fail here")
                msg = (f"⚠️ SIDEWAYS: last {len(w)} min range {span:.0f} pts\n"
                       f"   Resistance {hi:,.1f}{self.sp(hi)} | Support {lo:,.1f}{self.sp(lo)}\n   {where}")
                lines.append(msg)
                self._warn.append(f"⚠️ SIDEWAYS market (range {lo:,.0f}-{hi:,.0f}) - {where}")

        # D) LONG MOVE without a pullback -> be careful
        w = self.hist[-60:]
        if len(w) >= 10:
            ih = max(range(len(w)), key=lambda k: w[k]["h"])
            il = min(range(len(w)), key=lambda k: w[k]["l"])
            fall = w[ih]["h"] - c
            rise = c - w[il]["l"]
            if fall >= LONG_MOVE and ih < len(w) - 1:
                after = w[ih:]
                low_i = min(range(len(after)), key=lambda k: after[k]["l"])
                bounce = max(x["h"] for x in after[low_i:]) - after[low_i]["l"]
                if bounce < 0.25 * fall:
                    m = (f"⚠️ BE CAREFUL: price already fell {fall:.0f} pts since {w[ih]['t']} without a real "
                         f"pullback - a new PE is late, a CE is against the trend")
                    lines.append(m)
                    self._warn.append(m)
            if rise >= LONG_MOVE and il < len(w) - 1:
                after = w[il:]
                hi_i = max(range(len(after)), key=lambda k: after[k]["h"])
                dip = after[hi_i]["h"] - min(x["l"] for x in after[hi_i:])
                if dip < 0.25 * rise:
                    m = (f"⚠️ BE CAREFUL: price already rose {rise:.0f} pts since {w[il]['t']} without a real "
                         f"pullback - a new CE is late, a PE is against the trend")
                    lines.append(m)
                    self._warn.append(m)

    # ---------------------------------------------------------------- signals
    def _find_signal(self, t, o, h, l, c, d, trend, big, near, avg_r, guesses):
        a, b = self.hist[-2], self.hist[-1]
        lv = self.levels()
        gdirs = {g[0]: g[1] for g in guesses}
        cands = []
        _, avg_v, _ = self._avgs()
        for p, name in lv:
            if round(p) in self.used_levels:
                continue
            # REVERSAL UP at support: tested (low near/just under level), held (close above), confirmed
            low2 = min(a["l"], b["l"])
            if p - 2 * near <= low2 <= p + near and c > p and c > a["h"]:
                why = None
                if gdirs.get("ABSORPTION") == "UP" or gdirs.get("EXHAUSTION") == "UP":
                    why = "sellers absorbed / exhausted"
                elif d >= big and c > o:
                    why = f"strong buying {_k(d)} at the level"
                elif a["d"] <= -big and d > 0:
                    why = f"selling {_k(a['d'])} failed, buyers back {_k(d)}"
                if why:
                    stop = min(low2, p) - 3
                    cands.append(("CE", "REVERSAL", p, name, why, stop))
            hi2 = max(a["h"], b["h"])
            if p - near <= hi2 <= p + 2 * near and c < p and c < a["l"]:
                why = None
                if gdirs.get("ABSORPTION") == "DOWN" or gdirs.get("EXHAUSTION") == "DOWN":
                    why = "buyers absorbed / exhausted"
                elif d <= -big and c < o:
                    why = f"strong selling {_k(d)} at the level"
                elif a["d"] >= big and d < 0:
                    why = f"buying {_k(a['d'])} failed, sellers back {_k(d)}"
                if why:
                    stop = max(hi2, p) + 3
                    cands.append(("PE", "REVERSAL", p, name, why, stop))
            # BREAKOUT: close through the level with delta + cum delta + candle aligned
            vol_ok = b["v"] >= avg_v
            if a["c"] <= p < c and d >= big and c > o and "RISING" in trend and vol_ok:
                cands.append(("CE", "BREAKOUT", p, name, f"broke up with delta {_k(d)}, cum delta rising", p - near))
            if a["c"] >= p > c and d <= -big and c < o and "FALLING" in trend and vol_ok:
                cands.append(("PE", "BREAKOUT", p, name, f"broke down with delta {_k(d)}, cum delta falling", p + near))
        if self._explosive:
            sg, mid, why = self._explosive
            cands.insert(0, ("CE" if sg > 0 else "PE", "EXPLOSIVE", c, "explosive candle", why, mid))
        best = None
        for side, kind, p, name, why, stop in cands:
            risk = abs(c - stop)
            if risk < 5 or risk > MAX_RISK:
                continue
            sgn = 1 if side == "CE" else -1
            ahead = sorted(q for q, _ in lv if sgn * (q - c) >= 1.5 * risk)
            nxt = (ahead[0] if sgn > 0 else ahead[-1]) if ahead else None
            target = nxt if nxt is not None else c + sgn * 2 * risk
            if abs(target - c) < MIN_TARGET:
                target = c + sgn * max(MIN_TARGET, 1.5 * risk)
                nxt = None
            tname = next((n for q, n in lv if q == nxt), "2x risk") if nxt is not None else "2x risk"
            s = dict(t=t, side=side, kind=kind, level=p, lname=name, why=why, entry=c,
                     stop=stop, target=target, tname=tname, risk=risk, best=0.0)
            if kind == "EXPLOSIVE":          # highest priority; let the trailing stop ride it
                s["target"] = c + sgn * max(3 * risk, 80)
                s["tname"] = "3x risk (trailing stop rides the move)"
                return s
            if best is None or risk < best["risk"]:
                best = s
        return best

    def _track(self, t, h, l, c):
        s = self.signal
        sgn = 1 if s["side"] == "CE" else -1
        held = _mins(t) - _mins(s["t"])
        adv = l if sgn > 0 else h
        fav = h if sgn > 0 else l
        if sgn * (adv - s["stop"]) <= 0:
            out, why = s["stop"], ("TRAILING STOP hit" if s.get("trailing") else "STOP hit")
        elif sgn * (fav - s["target"]) >= 0:
            out, why = s["target"], "TARGET hit"
        elif held >= SIGNAL_TIMEOUT or t >= "15:15":
            out, why = c, f"{SIGNAL_TIMEOUT}-min time exit"
        else:
            s["best"] = max(s["best"], sgn * (fav - s["entry"]))
            if s["best"] >= TRAIL_START:
                ns = s["entry"] + sgn * (s["best"] - TRAIL_GAP)
                if sgn * (ns - s["stop"]) > 0:
                    s["stop"], s["trailing"] = ns, True
            return None
        pts = sgn * (out - s["entry"])
        # ATM option ~0.5 x futures move, minus charges/slippage (same model as our backtests)
        rs = (pts * 0.5 - min(8, held / 180 * 7)) * LOT - 125
        s.update(exit_t=t, exit=out, pts=pts, rs=rs, result=why, held=held)
        self.signals.append(s)
        self.signal = None
        self.last_exit = t
        self.used_levels.add(round(s["level"]))
        if self.on_signal:
            self.on_signal(s)
        icon = "✅" if rs > 0 else "🔴"
        return (f"{icon} SIGNAL RESULT  BUY {s['side']} ({s['kind']}) from {s['t']}\n"
                f"{why} at {t} | FUT {s['entry']:,.1f} → {out:,.1f} = {pts:+.1f} pts in {held} min\n"
                f"≈ {'+' if rs >= 0 else '-'}₹{abs(rs):,.0f} per lot on ATM {s['side']} (estimate after costs)\n"
                f"Signals today: {self.signal_score()}")

    def _sig_open_text(self, s):
        s["warnings"] = list(self._warn)
        sp = (f"\nNIFTY spot ≈ {s['entry'] - self.basis:,.0f} (futures premium {self.basis:+.0f})"
              if self.basis is not None else "")
        head = ("⭐🚨 HIGH-PROBABILITY SIGNAL" if s["kind"] == "EXPLOSIVE" else "🚨 ORDER-FLOW SIGNAL")
        setup = ("EXPLOSIVE breakout (delta 5x+ normal AND big candle)" if s["kind"] == "EXPLOSIVE"
                 else f"{s['kind']} at {s['lname']} {s['level']:,.1f}{self.sp(s['level'])}")
        warn = ("\n" + "\n".join(s["warnings"])) if s["warnings"] else ""
        return (f"{head}: BUY {s['side']} (ATM)  - paper, not traded\n"
                f"Setup: {setup}\n"
                f"Why: {s['why']}\n"
                f"Entry FUT {s['entry']:,.1f} | Stop {s['stop']:,.1f} ({-s['risk']:.0f}) | "
                f"Target {s['target']:,.1f} ({abs(s['target'] - s['entry']):+.0f}, {s['tname']})"
                f"{sp}\nTrailing: after +{TRAIL_START} pts, stop follows {TRAIL_GAP} pts behind | "
                f"max {SIGNAL_TIMEOUT} min. Time {s['t']}{warn}")

    def signal_score(self):
        if not self.signals:
            return "none yet"
        w = sum(1 for s in self.signals if s["rs"] > 0)
        tot = sum(s["rs"] for s in self.signals)
        return f"{len(self.signals)} | wins {w} | est. {'+' if tot >= 0 else '-'}₹{abs(tot):,.0f}"

    # ---------------------------------------------------------------- text
    def _text(self, t, o, h, l, c, buy, sell, d, cum, trend, lines):
        col = "green" if c > o else "red" if c < o else "doji"
        who = "🟢 BUYERS stronger" if d > 0 else "🔴 SELLERS stronger" if d < 0 else "⚪ balanced"
        ratio = (f" ({buy / sell:.1f}x)" if d > 0 and sell else f" ({sell / buy:.1f}x)" if d < 0 and buy else "")
        agree = ""
        if d and "flat" not in trend and col != "doji":
            up = d > 0 and "RISING" in trend and col == "green"
            dn = d < 0 and "FALLING" in trend and col == "red"
            agree = "\n💪 Delta + Cum Δ + candle all UP" if up else "\n💪 Delta + Cum Δ + candle all DOWN" if dn else ""
        cicon = "🟩 GREEN" if c > o else "🟥 RED" if c < o else "⬜ DOJI"
        who = ("Delta: BUYERS stronger" if d > 0 else "Delta: SELLERS stronger" if d < 0 else "Delta: balanced")
        spot = (f" | spot ≈ {c - self.basis:,.1f} (premium {self.basis:+.1f})" if self.basis is not None else "")
        nxt = self._next_levels(c)
        return (f"🕯 {t}  {cicon} candle ({c - o:+.1f} pts)  {self.symbol}\n"
                f"FUT O {o:,.1f}  H {h:,.1f}  L {l:,.1f}  C {c:,.1f}{spot}\n"
                f"Sell {_k(sell)} | Buy {_k(buy)} | Delta {d:+,}\n"
                f"Cum Δ {cum:+,} ({trend})\n{who}{ratio}{agree}{nxt}"
                + ("\n" + "\n".join(lines) if lines else ""))

    def _next_levels(self, c):
        """'Next support / resistance' line in SPOT terms (pivots + prev-day levels + profile)."""
        if self.basis is None:
            return ""
        s = c - self.basis
        lv = [(v, f"Pivot {k}") for k, v in self.pivots.items()]
        lv += [(p - self.basis, n) for p, n in self.static]
        poc, val, vah = value_area(self.profile)
        if poc is not None and len(self.hist) >= 15:
            lv += [(poc - self.basis, "Today POC"), (val - self.basis, "Today VAL"), (vah - self.basis, "Today VAH")]
        below = [x for x in lv if x[0] < s - 2]
        above = [x for x in lv if x[0] > s + 2]
        parts = []
        if below:
            v, n = max(below)
            parts.append(f"⬇️ support {n} {v:,.0f} ({v - s:+.0f})")
        if above:
            v, n = min(above)
            parts.append(f"⬆️ resistance {n} {v:,.0f} ({v - s:+.0f})")
        side = ""
        if "P" in self.pivots:
            side = " | " + ("above P = CE side" if s > self.pivots["P"] else "below P = PE side")
        return ("\n🗺 " + " | ".join(parts) + side) if parts else ""

    def close_open_signal(self, t, c):
        """End of day: close a still-open signal at the last price."""
        if self.signal:
            self.signal["t_force"] = True
            return self._track("15:15" if t < "15:15" else t, c, c, c)
        return None

    def day_summary(self):
        out = []
        for k, v in self.score.items():
            out.append(f"{k}: {v[0]}/{v[1]} right ({100 * v[0] / v[1]:.0f}%)")
        out.append(f"Signals: {self.signal_score()}")
        for s in self.signals:
            out.append(f"  {s['t']} {s['side']} {s['kind']} @ {s['lname']}: {s['result']} {s['pts']:+.1f} pts "
                       f"≈ ₹{s['rs']:+,.0f}")
        return out


def _mins(t):
    h, m = t.split(":")
    return int(h) * 60 + int(m)
