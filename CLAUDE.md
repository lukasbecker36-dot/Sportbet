# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Sportbet** is a Python pipeline that scrapes FotMob's internal API for historical xG (expected goals) shot data, detects xG patterns that precede goals, cross-references with Betfair in-play historical prices, and identifies +EV betting opportunities on over-goals markets. Phase 2 adds a live alert system.

## Tech Stack

- **Python 3.11+**
- **HTTP:** `requests` (no Selenium)
- **Data:** `pandas`, `numpy`
- **Storage:** SQLite via `sqlite3` (no ORM)
- **Betfair:** `betfairlightweight`
- **Scheduling (live):** `APScheduler` or `time.sleep` loop
- **Notifications (live):** `python-telegram-bot`

All dependencies tracked in `requirements.txt`.

## Project Structure

```
project/
├── config.py                  # API keys, thresholds, league IDs — in .gitignore
├── data/
│   ├── raw_json/              # Cached raw FotMob API responses
│   ├── betfair_historical/    # Downloaded Betfair historical price files
│   └── db/xg_signals.db      # Main SQLite database
├── scrapers/
│   ├── fotmob.py              # FotMob API wrapper
│   └── betfair_historical.py # Betfair historical file parser
├── pipeline/
│   ├── ingest.py              # Orchestrates scraping → DB writes
│   ├── signals.py             # xG signal detection logic
│   ├── backtest.py            # Backtesting engine / grid search
│   └── ev_analysis.py        # EV calculation and results output
├── live/
│   ├── monitor.py             # Live match polling loop (Phase 2)
│   └── alerts.py              # Telegram notification sender (Phase 2)
├── results/                   # Output CSVs and charts from backtest
└── notebooks/exploration.ipynb
```

## Development Order

Build and test strictly in this sequence — don't skip ahead:

1. `scrapers/fotmob.py` — fetch one match, parse shotmap, print to console
2. DB setup — create schema, insert that one match
3. `pipeline/ingest.py` — loop over all matches for one league/season
4. `pipeline/signals.py` — compute minute-by-minute features for all matches
5. `pipeline/backtest.py` — grid search across signal thresholds
6. Betfair data — parse one historical file, join to a match
7. `pipeline/ev_analysis.py` — full EV calculation with real Betfair odds
8. `live/monitor.py` — live polling loop (only after step 7 confirms edge)
9. `live/alerts.py` — Telegram notifications

## FotMob API

### Endpoints

```
GET https://www.fotmob.com/api/leagues?id={league_id}&season={season}
GET https://www.fotmob.com/api/matchDetails?matchId={match_id}
GET https://www.fotmob.com/api/matches?date={YYYYMMDD}   # live polling
```

### Required Headers

```python
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-GB,en;q=0.9',
    'Referer': 'https://www.fotmob.com/',
    'Origin': 'https://www.fotmob.com',
}
```

### Caching Policy

Always write raw JSON to `data/raw_json/{match_id}.json` before parsing. Always check for the cache file before making an HTTP request. This allows full re-parsing without re-scraping.

### Rate Limiting

- 2–3 s between match detail requests (`random.uniform(2, 3)`)
- 5 s between league/season index requests
- On 429 or 403: back off 60 s and retry once
- Log every request with timestamp to `scrape.log`

### Shotmap Parsing

Shotmap lives at `data['content']['shotmap']['shots']`. Relevant fields:

| Field | Notes |
|---|---|
| `min` | minute |
| `minAdded` | added time (may be null) |
| `expectedGoals` | xG |
| `isHomeTeam` | boolean |
| `playerName` | |
| `eventType` | `'Goal'`, `'Miss'`, `'SavedShot'`, `'BlockedShot'` |

Reconstruct cumulative xG and scoreline by iterating shots in minute order. Increment team score when `eventType == 'Goal'`. Skip matches with no shotmap gracefully (log warning, don't crash).

## Database Schema

```sql
CREATE TABLE matches (
    match_id    TEXT PRIMARY KEY,
    date        TEXT,
    league      TEXT,
    season      TEXT,
    home_team   TEXT,
    away_team   TEXT,
    home_goals  INTEGER,
    away_goals  INTEGER,
    total_goals INTEGER
);

CREATE TABLE shots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id            TEXT,
    minute              INTEGER,
    added_time          INTEGER DEFAULT 0,
    team                TEXT,   -- 'home' or 'away'
    player              TEXT,
    xg                  REAL,
    is_goal             INTEGER,
    shot_type           TEXT,
    cumulative_xg_home  REAL,
    cumulative_xg_away  REAL,
    cumulative_xg_total REAL,
    score_home          INTEGER,
    score_away          INTEGER,
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE TABLE signals (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id                TEXT,
    trigger_minute          INTEGER,
    signal_type             TEXT,
    signal_value            REAL,
    score_at_trigger        TEXT,
    goals_total_at_trigger  INTEGER,
    next_goal_within_10     INTEGER,
    next_goal_within_15     INTEGER,
    next_goal_within_20     INTEGER,
    next_goal_within_30     INTEGER,
    betfair_over05_odds     REAL,
    betfair_over15_odds     REAL,
    betfair_over25_odds     REAL,
    betfair_over35_odds     REAL,
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);
```

## Signal Detection

Minute-by-minute DataFrame features per match:

```python
xg_rate_10m       = xg accumulated in last 10 minutes (both teams)
xg_rate_15m       = xg accumulated in last 15 minutes
xg_rate_20m       = xg accumulated in last 20 minutes
cumulative_xg_total = total xG from kickoff to this minute
xg_deficit        = abs(cumulative_xg_home - cumulative_xg_away)
goals_scored      = current total goals
minutes_remaining = 90 - current_minute
xg_per_goal       = cumulative_xg_total / max(goals_scored, 1)
```

### Backtest Grid Search Parameters

```python
XG_RATE_THRESHOLDS = [0.20, 0.25, 0.30, 0.35, 0.40, 0.50]
MIN_MINUTE         = [30, 45, 55, 60]
MAX_MINUTE         = [75, 80, 85]
MIN_ODDS           = [1.30, 1.40, 1.50, 1.60, 1.80, 2.00]
MARKETS            = ['over_0.5', 'over_1.5', 'over_2.5', 'over_3.5']
```

Output sorted by EV; flag rows with `EV > 0.05` as candidate strategies.

## EV Formula

```
EV = (P_goal_within_window * net_decimal_odds) - 1
net_odds = (odds - 1) * 0.98 + 1   # 2% Betfair commission
```

Apply a 5–10% odds haircut to account for in-play price movement.

## Betfair Historical Data

Downloaded manually from `https://historicdata.betfair.com/` (free account required). Files are bz2-compressed CSV:

```python
import bz2, csv
with bz2.open(filepath, 'rt') as f:
    reader = csv.DictReader(f)
```

Key columns: `MARKET_TIME`, `SELECTION_NAME`, `LAST_PRICE_TRADED`, `MATCHED_AMOUNT`.

Match to FotMob records on: home team + away team + date. Team names differ between FotMob and Betfair — maintain a `TEAM_NAME_MAP` dict in `config.py` as mismatches are found.

Betfair over-goals markets are named e.g. `"Over/Under 2.5 Goals"` with selections `"Over 2.5 Goals"` / `"Under 2.5 Goals"`.

## Config Structure (`config.py`)

```python
BETFAIR_USERNAME    = ''
BETFAIR_PASSWORD    = ''
BETFAIR_APP_KEY     = ''
TELEGRAM_BOT_TOKEN  = ''
TELEGRAM_CHAT_ID    = ''

SCRAPE_DELAY_MIN = 2.0
SCRAPE_DELAY_MAX = 3.0
DB_PATH          = 'data/db/xg_signals.db'
RAW_CACHE_DIR    = 'data/raw_json/'
BETFAIR_DATA_DIR = 'data/betfair_historical/'

SEASONS  = ['2021/2022', '2022/2023', '2023/2024', '2024/2025']
LEAGUE_IDS = [47, 48, 54, 55]  # PL, Championship, Bundesliga, Serie A

LEAGUES = {
    'premier-league': 47,
    'championship':   48,
    'la-liga':        87,
    'bundesliga':     54,
    'serie-a':        55,
    'ligue-1':        53,
    'eredivisie':     57,
}

TEAM_NAME_MAP = {}  # populated as FotMob↔Betfair mismatches are found
```

`config.py` must be in `.gitignore`.

## Expected Outputs

- `results/signal_ev_table.csv` — full grid search results
- `results/best_signals.csv` — filtered to EV > 5%, n_triggers > 50
- `results/equity_curve.png` — cumulative P&L chart for top 3 strategies
- Console summary: best signal, win rate, avg odds, EV, recommended minimum odds

## Key Gotchas

- FotMob xG is a **post-match reconstruction** — ~30–60 s data lag in live use. Account for this in live trigger logic.
- xGOT (`expectedGoalsOnTarget`) is available per shot but not required for the primary signal — use it for exploratory analysis only.
- Some matches have no shotmap (cup games, lower leagues) — skip gracefully.
- Live phase (`live/`) is Phase 2: build only after backtest confirms a positive-EV signal. Do NOT auto-place bets in v1 — alerts only.
- Kelly criterion stake sizing is used in alert messages, not for automated placement.
