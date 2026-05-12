"""Configuration template. Copy to config.py and fill in secrets.

config.py is gitignored — never commit real credentials.
"""

# --- Secrets (live phase only; leave blank for the backtest pipeline) ---
BETFAIR_USERNAME = ""
BETFAIR_PASSWORD = ""
BETFAIR_APP_KEY = ""
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""

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
