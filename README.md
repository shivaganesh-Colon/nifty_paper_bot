# NIFTY Option-Buying Paper Trading Bot

**Strategy:** OI levels decide **WHERE**, price action decides **WHEN**.

- NIFTY pushes into the strongest **Call-OI strike** (resistance) and fails → **buy PE**
- NIFTY pushes into the strongest **Put-OI strike** (support) and holds → **buy CE**

This version is **paper trading only**. It uses real live data but never places real orders.

---

## Files

| File | What it does |
|---|---|
| `config.py` | **All settings.** The only file you normally edit |
| `login.py` | Daily Zerodha login (run each morning) |
| `bot.py` | Main bot: runs the whole day |
| `market.py` | Reads prices, candles and option contracts from Zerodha |
| `levels.py` | Option chain scanner: OI support/resistance, writers adding/leaving, PCR |
| `setups.py` | The 4 price-action setups (failed breakout, double top/bottom, lower high/higher low, rejection candle) |
| `paper.py` | Paper trade engine: entry, stop-loss, target, P&L |
| `report.py` | Win rate and P&L report |
| `simulate.py` | Practice run on a made-up day (no Zerodha needed) |

---

## One-time setup

1. **Install Python 3.10 or newer** (python.org). On Windows, tick "Add Python to PATH".
2. Put all these files in one folder, open a terminal in that folder and run:
   ```
   pip install -r requirements.txt
   ```
3. **Try the simulator first** (works today, no account needed):
   ```
   python simulate.py          # V-shape day -> reversal works
   python simulate.py trend    # trend day -> stop-loss protects you
   ```
4. **Create your Kite Connect app** at https://developers.kite.trade
   - Subscribe to the **Connect** plan (needed for live prices and OI)
   - Redirect URL: `http://127.0.0.1`
   - Copy the **API key** and **API secret** into `config.py`

## Every trading day

```
python login.py      # before 9:15 - open the link, log in, paste the request_token
python bot.py        # start before 9:15 and leave it running
```

The bot stops by itself after 15:15 and prints the day's report.
To stop early: press **Ctrl+C**, or create an empty file named `STOP` in the folder
(delete it before the next day).

## Telegram alerts (optional)

1. In Telegram, open **@BotFather**, send `/newbot`, pick a name and a username ending in `bot`. Copy the **token** it gives you.
2. Run `python telegram_setup.py`, paste the token, then tap **START** on your new bot in Telegram and press Enter.
3. You get a test message, and the settings are saved into `config.py` automatically.

You then get alerts for: bot start, the day's OI levels, every paper BUY (strike, entry, stop-loss, target, reasons), every exit with P&L, the end-of-day summary, and any crash.
Set `ALERT_SKIPPED_SIGNALS = True` in `config.py` to also get the signals the bot skipped.
`python simulate.py` sends its alerts too (marked `[SIMULATION]`), which makes a good test.

## What you get

| Where | What |
|---|---|
| `logs/bot_DATE.log` | Everything the bot saw and did, minute by minute |
| `logs/trades.csv` | Every paper trade: setup, reasons, entry/exit, P&L |
| `logs/signals.csv` | Every signal, **including skipped ones and why** |
| `data/chain_DATE.csv` | Option chain snapshot every minute (your own data for backtesting) |
| `data/nifty_5min_DATE.csv` | NIFTY candles for the day |

See the overall result any time:
```
python report.py
```

## How a trade is decided

1. **Levels:** top Call-OI strikes = resistance, top Put-OI strikes = support
2. **Setup:** one of the 4 price-action patterns appears **at** a level
3. **Score** (need 3 or more by default):
   - setup found: +1
   - writers holding at the level: +1, or adding: +2
   - strong 100+ point move into the level: +1
   - previous-day high/low at the same level: +1
   - PCR trend agrees: +1
4. **Vetoes:** writers leaving the level, stop too tight or too far, daily limits reached
5. **Entry:** ATM option, 1 lot. **Stop:** NIFTY beyond the pattern's extreme. **Target:** 1x risk.

## Safety limits (in `config.py`)

- Max 2 trades a day, max 1 CE and 1 PE attempt
- Stop for the day after 2 losses or Rs 4,000 loss
- No new trades before 9:45 or after 14:30, everything closed by 15:15
- Skips big-gap days (possible trend days)

## Important

- Run paper mode for **at least 15-20 trading days** (30+ trades) before judging the win rate.
- Paper fills use real bid/ask plus slippage, but real fills can still differ.
- The simulator uses **synthetic** prices to show how the bot thinks. Its result says nothing about real profitability.
