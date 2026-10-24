"""
Paper trading report: win rate, average win/loss, P&L by setup.

    python report.py              # all days
    python report.py 2026-10-19   # one day
"""
import csv
import os
import sys
from collections import defaultdict


def report(trades_file="logs/trades.csv", day=None):
    if not os.path.exists(trades_file):
        print("No trades yet.")
        return
    with open(trades_file) as f:
        rows = [r for r in csv.DictReader(f) if day is None or r["date"] == day]
    if not rows:
        print("No trades for that period.")
        return

    def stats(rs):
        nets = [float(r["net_pnl"]) for r in rs]
        wins = [n for n in nets if n > 0]
        losses = [n for n in nets if n <= 0]
        return {
            "trades": len(nets), "wins": len(wins), "losses": len(losses),
            "win_rate": 100 * len(wins) / len(nets),
            "avg_win": sum(wins) / len(wins) if wins else 0,
            "avg_loss": sum(losses) / len(losses) if losses else 0,
            "net": sum(nets),
        }

    s = stats(rows)
    days = sorted({r["date"] for r in rows})
    print("=" * 56)
    print(f" PAPER TRADING REPORT  {days[0]} to {days[-1]}  ({len(days)} days)")
    print("=" * 56)
    print(f" Trades      : {s['trades']}   (wins {s['wins']} / losses {s['losses']})")
    print(f" Win rate    : {s['win_rate']:.0f}%")
    print(f" Avg win     : Rs {s['avg_win']:.0f}")
    print(f" Avg loss    : Rs {s['avg_loss']:.0f}")
    print(f" Net P&L     : Rs {s['net']:.0f}   (after estimated charges)")

    by = defaultdict(list)
    for r in rows:
        by[r["setup"]].append(r)
    print("\n By setup:")
    for name, rs in sorted(by.items()):
        x = stats(rs)
        print(f"   {name:<18} {x['trades']:>3} trades  win {x['win_rate']:>4.0f}%  net Rs {x['net']:>8.0f}")

    print("\n Trades:")
    for r in rows:
        print(f"   {r['date']} {r['entry_time']}  {r['direction']}  {r['setup']:<17} "
              f"{r['exit_reason']:<26} Rs {float(r['net_pnl']):>7.0f}  {r['result']}")
    if s["trades"] < 30:
        print(f"\n Note: only {s['trades']} trades - too few to trust the win rate yet (aim for 30+).")


if __name__ == "__main__":
    report(day=sys.argv[1] if len(sys.argv) > 1 else None)
