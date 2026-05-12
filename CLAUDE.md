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
config.example.py             # template; copy to config.py (gitignored) before running
config.py                     # API keys, thresholds, league/season config — in .gitignore
run.py                        # CLI entrypoint (init-db | ingest | signals | backtest | ev | all)
db/
  schema.sql                  # matches / shots / signals tables + indexes
  connection.py               # get_conn(), init_db()
scrapers/
  fotmob.py                   # FotMob API wrapper: session+headers, cached GET, league/match, parse_match
  betfair_historical.py       # bz2-CSV parser, over-goals price extraction, net_odds()
pipeline/
  ingest.py                   # FotMob -> matches + shots tables (idempotent; uses raw_json cache)
  signals.py                  # minute-by-minute feature frame; crossing-event detection -> signals table
  backtest.py                 # grid search over thresholds/windows/markets -> results/signal_ev_table.csv
  ev_analysis.py              # attach Betfair odds (if files present), EV calc, best_signals + equity curve
utils/
  logging.py                  # get_logger() -> scrape.log + console
  teams.py                    # normalise() / canonical() for FotMob<->Betfair name matching
live/                         # Phase 2 (not yet built): monitor.py, alerts.py
data/raw_json/                # cached raw FotMob responses (gitignored)
data/betfair_historical/      # drop downloaded Betfair price files here (gitignored)
data/db/xg_signals.db         # main SQLite db (gitignored)
results/                      # signal_ev_table.csv, best_signals.csv, equity_curve.png (gitignored)
notebooks/exploration.ipynb   # EDA stub
tests/                        # pytest: shotmap parse, signals, betfair parse
```

## Commands

```bash
pip install -r requirements.txt
cp config.example.py config.py        # then edit league/season scope as needed

python run.py init-db                 # create the SQLite schema
python run.py ingest                  # scrape FotMob -> matches + shots (respects raw_json cache)
python run.py signals                 # compute xG signals -> signals table
python run.py backtest                # grid search -> results/signal_ev_table.csv
python run.py ev                       # attach Betfair odds (if any) + write best_signals / equity curve
python run.py all                     # init-db -> ingest -> signals -> backtest -> ev

pytest -q                             # run all tests
pytest tests/test_signals.py::test_xg_rate_window_values   # run one test
```

## Current State (Phase 1: backtest pipeline, steps 1–7)

Steps 1–7 are implemented. The `live/` package (steps 8–9) is intentionally not built yet — per the
brief, build it only after a backtest confirms a positive-EV signal.

**Known limitation:** FotMob's API requires browser-like requests; if you hit persistent `403`s, the
host is being blocked (network egress / anti-bot). The scraper handles 429/403 with a 60s backoff +
one retry, then raises. The rest of the pipeline (`signals` → `backtest` → `ev`) runs entirely on the
local SQLite DB and is independent of network access.

**No Betfair files yet:** `python run.py ev` runs in "no-odds mode" — it re-emits the win-rate table
and prints a note. Drop `.bz2`/`.csv` price files into `data/betfair_historical/` and re-run `ev` to
populate the `signals.betfair_*_odds` columns and get real EV / profit / sharpe figures.

## Development Order (from the brief)

1. `scrapers/fotmob.py` — fetch one match, parse shotmap, print to console *(done)*
2. DB setup — `db/schema.sql` + `db/connection.py` *(done)*
3. `pipeline/ingest.py` — loop over matches for a league/season *(done)*
4. `pipeline/signals.py` — minute-by-minute features for all matches *(done)*
5. `pipeline/backtest.py` — grid search across signal thresholds *(done)*
6. Betfair data — `scrapers/betfair_historical.py` parser *(done)*
7. `pipeline/ev_analysis.py` — EV calculation with Betfair odds *(done; odds path runs once files exist)*
8. `live/monitor.py` — live polling loop *(not started)*
9. `live/alerts.py` — Telegram notifications *(not started)*

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

## Implementation Decisions (read before changing signals/backtest)

- **Signal = crossing event.** `signals.detect_signals` emits one row per match at the minute the
  `xg_rate_15m` series transitions from below `min(XG_RATE_THRESHOLDS)` to at-or-above it (within
  `[min(MIN_MINUTE), max(MAX_MINUTE)]`). At most a few rows per match → roughly independent samples.
  The backtest re-applies higher thresholds and tighter minute windows by filtering on the stored
  `signal_value` / `trigger_minute` — so the `signals` table stays small and the grid search is a
  pure pandas filter.
- **Over-line win modelling.** A trigger at minute `m` with `G` goals already scored is evaluated
  **only** against the `over-(G+0.5)` line (the live-relevant case); other markets are skipped for
  that trigger. It "wins" if ≥1 more goal is scored within `DEFAULT_GOAL_WINDOW` minutes of `m`
  (`signals.next_goal_within_<W>`), or trivially if the line was already cleared.
- **Odds adjustment.** `net_odds = (odds-1)*(1-BETFAIR_COMMISSION)+1`, then a conservative haircut on
  the edge: `adjusted = 1 + (net_odds-1)*ODDS_HAIRCUT`. EV/profit/sharpe use `adjusted`.
- **Betfair odds path is scaffold.** Without price files, `ev_analysis` runs in no-odds mode. With
  files, event↔match alignment is best-effort (team-name match on `(home, away, date)`; a mid-match
  price snapshot proxies the trigger-minute price). Validate against real files before trusting EV.
