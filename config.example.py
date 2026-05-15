"""Configuration template. Copy to config.py and fill in secrets.

config.py is gitignored — never commit real credentials.
"""

# --- Secrets (live phase only; leave blank for the backtest pipeline) ---
BETFAIR_USERNAME = ""
BETFAIR_PASSWORD = ""
BETFAIR_APP_KEY = ""
# Path to a directory containing client-2048.crt + client-2048.key. Required
# only when running from a datacenter IP (Hetzner, AWS etc.) since Betfair
# 403s the interactive login from cloud ranges. Leave empty on a residential
# IP to use interactive (username/password) login.
BETFAIR_CERTS_PATH = ""
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""

# Optional residential proxy for SofaScore traffic ONLY. Required when running
# on a datacenter IP (Hetzner/AWS/DigitalOcean) since Cloudflare 403s those
# IP ranges for SofaScore. Format: "http://user:pass@host:port".
# Betfair + Telegram continue to go direct — do NOT proxy those.
SOFASCORE_HTTP_PROXY = ""

# --- Scraping ---
SCRAPE_DELAY_MIN = 2.0
SCRAPE_DELAY_MAX = 3.0
LEAGUE_INDEX_DELAY = 5.0
BACKOFF_SECONDS = 60.0

# --- Paths ---
DB_PATH = "data/db/xg_signals.db"
RAW_CACHE_DIR = "data/raw_json/"
BETFAIR_DATA_DIR = "data/betfair_historical/"
RESULTS_DIR = "results/"
SCRAPE_LOG = "scrape.log"

# --- What to scrape (FotMob season string format: 'YYYY/YYYY') ---
SEASONS = ["2021/2022", "2022/2023", "2023/2024", "2024/2025"]
LEAGUE_IDS = [47, 48, 54, 55]  # Premier League, Championship, Bundesliga, Serie A

LEAGUES = {
    "premier-league": 47,
    "championship": 48,
    "la-liga": 87,
    "bundesliga": 54,
    "serie-a": 55,
    "ligue-1": 53,
    "eredivisie": 57,
}

# FotMob team name -> Betfair team name. Populate as mismatches are found.
TEAM_NAME_MAP = {}

# --- Signal / backtest grid ---
XG_RATE_THRESHOLDS = [0.20, 0.25, 0.30, 0.35, 0.40, 0.50]  # per 15-min window
MIN_MINUTE = [30, 45, 55, 60]
MAX_MINUTE = [75, 80, 85]
MIN_ODDS = [1.30, 1.40, 1.50, 1.60, 1.80, 2.00]
MARKETS = ["over_0.5", "over_1.5", "over_2.5", "over_3.5"]

# Window (minutes) used as the "next goal" horizon when evaluating a trigger.
DEFAULT_GOAL_WINDOW = 15

# Betfair commission + conservative in-play price haircut.
BETFAIR_COMMISSION = 0.02
ODDS_HAIRCUT = 0.92  # multiply decimal-odds edge by this to be conservative

# Candidate-strategy filters for results/best_signals.csv
MIN_EV = 0.05
MIN_TRIGGERS = 50

# --- Live phase (live/ package) ---
# Strategy defaults — used when SofaScore tournament id doesn't match any
# league-specific entry below. These mirror the OOS-robust pick for PL.
LIVE_XG_THRESHOLD = 0.20
LIVE_MARKET_LINE = 2.5
LIVE_MIN_MINUTE = 30
LIVE_MAX_MINUTE = 85
LIVE_BASE_WIN_RATE = 0.57

# Auto-discovery: when True the runner polls SofaScore for live + upcoming
# matches in any league in LIVE_STRATEGY_BY_LEAGUE and starts a monitor for
# each. You can still /watch <event_id> manually for matches outside this set.
LIVE_AUTO_DISCOVER = True
LIVE_AUTO_DISCOVER_INTERVAL_S = 60      # how often to scan
LIVE_AUTO_DISCOVER_LOOKAHEAD_H = 3      # also watch fixtures starting in next N hours

# Per-league overrides — keyed by SofaScore uniqueTournament id.
# Each tuple holds the robust strategy from the 24/25 + 25/26 holdout
# (results/per_league_best.csv) with the *averaged* win-rate used for EV.
#
#   (threshold, market_line, min_minute, max_minute, win_rate)
#
# Source: backtest min(EV across both seasons) ≥ 0.05 with n_priced ≥ 30/season.
LIVE_STRATEGY_BY_LEAGUE: dict[int, tuple] = {
    17: (0.20, 2.5, 55, 85, 0.572),  # ENG-Premier League
    8:  (0.30, 1.5, 55, 80, 0.590),  # ESP-La Liga
    23: (0.25, 3.5, 30, 85, 0.505),  # ITA-Serie A
    35: (0.20, 2.5, 30, 75, 0.783),  # GER-Bundesliga
    34: (0.50, 1.5, 30, 80, 0.755),  # FRA-Ligue 1
}

# EV gating: only fire the Telegram alert if both (a) the xG threshold is met
# AND (b) Betfair live odds give EV >= this floor after commission + haircut.
LIVE_MIN_EV = 0.10

# Bet placement caps (defence in depth).
LIVE_STAKE_GBP = 10.0           # flat per trade
LIVE_MAX_STAKE_GBP = 10.0       # hard cap; placement aborts if exceeded
LIVE_DAILY_STAKE_CAP_GBP = 30.0 # rough 3-trade-per-day ceiling
LIVE_CONFIRM_TIMEOUT_S = 60     # manual mode: seconds before alert expires
LIVE_POLL_SECONDS = 30

# Auto-place behaviour:
#   "manual"         — Telegram message with [Place £10] / [Skip] buttons.
#                      Nothing happens until you tap. Expires after
#                      LIVE_CONFIRM_TIMEOUT_S.
#   "cancel_window"  — Telegram message with [✖ CANCEL] only. After
#                      LIVE_AUTO_CANCEL_WINDOW_S seconds with no tap, the bet
#                      places automatically (re-priced EV recheck still runs).
#   "full_auto"      — Place immediately on signal. Telegram receives a
#                      result-only message. /kill is the only abort.
LIVE_AUTO_PLACE_MODE = "cancel_window"
LIVE_AUTO_CANCEL_WINDOW_S = 15

# Path of a file that, when present, disables ALL bet placement (alerts still
# fire). Touch this file as an emergency kill switch.
LIVE_KILL_SWITCH_FILE = "KILL"
