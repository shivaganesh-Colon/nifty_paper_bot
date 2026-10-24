"""
Settings for the NIFTY option-buying PAPER TRADING bot.
Strategy: OI levels decide WHERE to trade, price action decides WHEN.

  - Near the strongest Call-OI strike (resistance) -> look for a turn DOWN -> buy PE
  - Near the strongest Put-OI strike (support)     -> look for a turn UP   -> buy CE

This bot never places real orders. It uses live data and records the
trades it would have taken.
"""

# ---------------- Zerodha Kite Connect ----------------
# Create an app at https://developers.kite.trade (Connect plan needed for live data + OI)
API_KEY = "your_api_key"
API_SECRET = "your_api_secret"

# ---------------- Instrument ----------------
UNDERLYING = "NIFTY"            # option name in Zerodha's NFO instrument list
SPOT_SYMBOL = "NSE:NIFTY 50"    # index quote
SPOT_TOKEN = 256265             # NIFTY 50 index token (for candles)
STRIKE_STEP = 50
LOTS = 1                        # lot size itself is read from Zerodha automatically
TRADE_ON_EXPIRY_DAY = False     # False = on expiry day, use next week's options

# ---------------- Timing (IST, 24h) ----------------
MARKET_OPEN = "09:15"
NO_TRADE_BEFORE = "09:45"       # let the open settle and OI levels form
NO_NEW_TRADE_AFTER = "14:30"    # a reversal needs time to play out
EXIT_TIME = "15:15"             # close any open paper trade
CANDLE_MINUTES = 5              # price-action candle size (3, 5, 10 or 15)
LOOP_SECONDS = 2                # how often to check NIFTY price for SL/target
CANDLE_POLL_SECONDS = 15        # how often to look for a newly completed candle
CHAIN_REFRESH_SECONDS = 60      # how often to read the option chain (OI)

# ---------------- Option chain ("WHERE") ----------------
CHAIN_STRIKES_EACH_SIDE = 20    # strikes scanned on each side of ATM
TOP_OI_LEVELS = 2               # use the top-2 OI strikes on each side as levels
OI_TREND_MINUTES = 15           # look-back for "are writers adding or leaving?"
WRITERS_ADD_PCT = 2.0           # OI up >= 2%  -> writers adding (strong level)
WRITERS_EXIT_PCT = 3.0          # OI down >= 3% -> writers leaving -> NO trade
PCR_TREND_MIN = 0.05            # PCR change that counts as a bias

# ---------------- Price action ("WHEN") ----------------
NEAR_LEVEL_POINTS = 30          # how close price must come to the OI level
STRONG_MOVE_POINTS = 100        # a "strong move" into the level...
STRONG_MOVE_CANDLES = 9         # ...within this many candles (9 x 5min = 45min)
SWING_STRENGTH = 2              # candles on each side to confirm a swing high/low
PATTERN_LOOKBACK = 24           # candles searched for double top / lower high
BREAKOUT_MIN_POINTS = 5         # close this far beyond level = real breakout attempt
FAILED_BREAKOUT_CANDLES = 3     # breakout must fail within this many candles
DOUBLE_TOP_TOLERANCE = 15       # two tops within this many points = "same" top
DOUBLE_TOP_MIN_DIP = 30         # minimum dip between the two tops
LOWER_HIGH_MIN_POINTS = 10      # second high must be at least this much lower
WICK_RATIO = 0.5                # rejection candle: wick >= 50% of candle range

# ---------------- Trade quality filter ----------------
MIN_SCORE = 3                   # setup(1) + writers holding(1)/adding(2) + strong move(1)
                                # + previous-day high/low at level(1) + PCR agrees(1)
SL_BUFFER_POINTS = 5            # stop-loss placed this far beyond the extreme
MIN_RISK_POINTS = 10            # skip if stop is too tight (noise will hit it)
MAX_RISK_POINTS = 60            # skip if stop is too far (bad risk)
GAP_SKIP_PCT = 1.0              # skip the day if NIFTY gaps more than 1% (trend-day risk)

# ---------------- Trade management ----------------
TARGET_R = 1.0                  # target = 1 x risk (in NIFTY points) -> higher win rate
TRAIL_POINTS = 25               # trailing stop for the 2nd half (only when LOTS >= 2)
PREMIUM_SL_PCT = 35             # safety: exit if option premium falls 35%
MAX_TRADES_PER_DAY = 2
MAX_TRADES_PER_DIRECTION = 1    # one CE and one PE attempt max - stops fighting trend days
MAX_LOSSES_PER_DAY = 2
MAX_DAILY_LOSS_RUPEES = 4000

# ---------------- Paper trading realism ----------------
PAPER_SLIPPAGE_PCT = 0.5        # extra slippage on top of bid/ask
EST_CHARGES_PER_TRADE = 60      # rough brokerage + taxes per round trip (Rs)

# ---------------- Telegram alerts (optional) ----------------
# Easiest setup: run  python telegram_setup.py  - it fills these in for you.
TELEGRAM_ENABLED = False
TELEGRAM_BOT_TOKEN = "your_bot_token"
TELEGRAM_CHAT_ID = ""
ALERT_SKIPPED_SIGNALS = False   # True = also alert signals the bot skipped (more messages)
