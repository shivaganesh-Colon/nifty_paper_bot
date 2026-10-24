"""
Option chain scanner - the "WHERE".

Reads OI for strikes around ATM every minute and works out:
  - resistance = strikes with the highest Call OI
  - support    = strikes with the highest Put OI
  - whether option writers at a level are adding, holding or leaving
  - PCR trend (bias)
Every snapshot is also saved to data/ so you build your own history for backtesting.
"""
import csv
import os
from collections import deque
from datetime import timedelta

import config as C


class ChainTracker:
    def __init__(self, market, chain, record_file):
        self.m = market
        self.chain = chain
        self.file = record_file
        self.history = deque(maxlen=400)   # (time, spot, {strike: {...}})

    def refresh(self, spot):
        step = C.STRIKE_STEP
        atm = round(spot / step) * step
        lo, hi = atm - C.CHAIN_STRIKES_EACH_SIDE * step, atm + C.CHAIN_STRIKES_EACH_SIDE * step
        keys, meta = [], {}
        for (strike, typ), inst in self.chain.items():
            if lo <= strike <= hi:
                k = f"NFO:{inst['tradingsymbol']}"
                keys.append(k)
                meta[k] = (strike, typ)
        q = self.m.quote(keys)
        snap = {}
        for k, (strike, typ) in meta.items():
            d = q.get(k)
            if not d:
                continue
            row = snap.setdefault(strike, {"CE_oi": 0, "PE_oi": 0, "CE_ltp": 0, "PE_ltp": 0})
            row[f"{typ}_oi"] = d.get("oi", 0) or 0
            row[f"{typ}_ltp"] = d.get("last_price", 0) or 0
        now = self.m.clock.now()
        if snap:
            self.history.append((now, spot, snap))
            self._record(now, spot, snap)
        return snap

    def _record(self, now, spot, snap):
        new = not os.path.exists(self.file)
        with open(self.file, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "spot", "strike", "ce_oi", "pe_oi", "ce_ltp", "pe_ltp"])
            for s in sorted(snap):
                r = snap[s]
                w.writerow([now.strftime("%H:%M:%S"), spot, s,
                            r["CE_oi"], r["PE_oi"], r["CE_ltp"], r["PE_ltp"]])

    # ---------- levels ----------
    def levels(self, spot):
        """Returns (resistance_strikes, support_strikes), strongest first."""
        if not self.history:
            return [], []
        snap = self.history[-1][2]
        step = C.STRIKE_STEP
        calls = [(s, r["CE_oi"]) for s, r in snap.items() if s >= spot - 2 * step and r["CE_oi"] > 0]
        puts = [(s, r["PE_oi"]) for s, r in snap.items() if s <= spot + 2 * step and r["PE_oi"] > 0]
        return self._distinct(calls), self._distinct(puts)

    @staticmethod
    def _distinct(rows):
        """Top OI strikes, skipping neighbours of an already-chosen strike (same zone)."""
        picked = []
        for s, _ in sorted(rows, key=lambda x: -x[1]):
            if all(abs(s - p) > 2 * C.STRIKE_STEP for p in picked):
                picked.append(s)
            if len(picked) == C.TOP_OI_LEVELS:
                break
        return picked

    def _snap_ago(self, minutes):
        if len(self.history) < 2:
            return None
        target = self.history[-1][0] - timedelta(minutes=minutes)
        older = [h for h in self.history if h[0] <= target]
        return (older[-1] if older else self.history[0])[2]

    def oi_change_pct(self, strike, typ, minutes):
        old = self._snap_ago(minutes)
        new = self.history[-1][2] if self.history else None
        if not old or not new or strike not in old or strike not in new:
            return None
        o, n = old[strike][f"{typ}_oi"], new[strike][f"{typ}_oi"]
        if o <= 0:
            return None
        return (n - o) / o * 100

    def writers_status(self, strike, typ):
        """'adding' / 'holding' / 'exiting' / 'unknown' for writers at a strike."""
        ch = self.oi_change_pct(strike, typ, C.OI_TREND_MINUTES)
        if ch is None:
            return "unknown", None
        if ch <= -C.WRITERS_EXIT_PCT:
            return "exiting", ch
        if ch >= C.WRITERS_ADD_PCT:
            return "adding", ch
        return "holding", ch

    def pcr_trend(self, minutes):
        """Change in PCR over `minutes` (same strikes compared). + = bullish, - = bearish."""
        old = self._snap_ago(minutes)
        if not old:
            return None
        new = self.history[-1][2]
        common = [s for s in new if s in old]

        def pcr(snap):
            ce = sum(snap[s]["CE_oi"] for s in common)
            pe = sum(snap[s]["PE_oi"] for s in common)
            return pe / ce if ce else None

        a, b = pcr(old), pcr(new)
        return None if a is None or b is None else b - a
