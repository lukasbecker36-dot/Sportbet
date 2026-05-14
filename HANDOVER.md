# Sportbet — Project Handover

**Date:** 2026-05-14  
**Status:** Phase 1 complete (backtest pipeline). Phase 2 (live alerts) not started.

---

## What Was Built

The full backtest pipeline (brief steps 1–7):

| Module | What it does |
|---|---|
| `scrapers/fotmob.py` | FotMob API wrapper with caching (blocked on server; use locally) |
| `scrapers/statsbomb.py` | StatsBomb open-data adapter (GitHub raw; works on server) |
| `scrapers/betfair_historical.py` | Betfair streaming JSON + legacy CSV parser |
| `pipeline/ingest.py` | FotMob scrape → `matches` + `shots` tables |
| `pipeline/statsbomb.py` | StatsBomb ingest → same `matches` + `shots` schema |
| `pipeline/signals.py` | Minute-by-minute xG features → crossing events → `signals` table |
| `pipeline/backtest.py` | Grid search over thresholds / windows / markets → `results/signal_ev_table.csv` |
| `pipeline/ev_analysis.py` | Attach Betfair odds per signal (time-accurate), compute EV, write outputs |
| `run.py` | CLI: `init-db \| ingest \| signals \| backtest \| ev \| all` |
| `db/schema.sql` | Three tables: `matches`, `shots`, `signals` |
| `utils/teams.py` | Name normalisation for FotMob ↔ Betfair matching |

---

## Current Data State

### xG Data (StatsBomb open data)

| League | Season | Matches | Notes |
|---|---|---|---|
| Premier League | 2015/16 | 380 | Full season ✓ |
| Serie A | 2015/16 | 380 | Full season ✓ |
| La Liga | 2015/16 | 380 | Full season ✓ |
| Ligue 1 | 2015/16 | 377 | Full season ✓ |
| Bundesliga | 2015/16 | 306 | Full season ✓ |
| La Liga | 2004/05–2020/21 | ~350 | Barcelona matches only — not representative |
| Bundesliga | 2023/24 | 34 | Partial |
| Ligue 1 | 2021/22–2022/23 | 58 | Partial |

**Total: 2,403 matches, 60,209 shots, 4,191 signals**

**Critical limitation:** All full-season data is 2015/16. There is no full out-of-sample season — all EV figures should be treated as **in-sample** until validated.

### Betfair Odds Data

- **Files:** `data/betfair_historical/2021/` — 28,171 `.bz2` streaming JSON files (2020/21 season, UK over-goals markets)
- **Matched:** 1,283 signals across 661 matches have at least one odds column populated
- **Coverage:** La Liga, Ligue 1, Bundesliga, Serie A, Premier League 2015/16 (matched by team+date), plus some La Liga 2020/21 Barcelona matches
- **Note:** Date-matching uses ±1 day UTC tolerance; any mis-matches would produce wrong odds. Validate sample size before trusting EV.

---

## Key Results

### Top 3 Strategies (`results/best_signals.csv`, 123 rows passing EV > 0.05, n ≥ 50)

| Signal | Threshold | Market | Window | n | Win% | Avg Odds | EV | Profit (1u) | Sharpe |
|---|---|---|---|---|---|---|---|---|---|
| xg_rate_15m | 0.25 | over_3.5 | 55–80 min | 51 | 66.7% | 3.42 | **1.17** | +59.4u | 0.46 |
| xg_rate_15m | 0.25 | over_3.5 | 60–85 min | 57 | 52.6% | 5.73 | **1.06** | +60.5u | 0.34 |
| xg_rate_15m | 0.25 | over_3.5 | 55–85 min | 66 | 57.6% | 5.21 | **1.01** | +66.5u | 0.35 |

> **Flat £10 stake interpretation:** Best strategy (+59.4 units over 51 bets) = ~**+£594** on a £510 outlay.

### Observations

- The strongest signal is **high xG pressure (≥0.25 xG/15min) between 55–80 min triggering an over-3.5 goals bet** — this makes intuitive sense: late in a game, 3+ goals already scored, both teams attacking.
- `over_2.5` strategies (min 30–45) also show consistent EV ~0.50–0.90 with larger samples (80–170 triggers).
- High-EV rows with n < 50 (EV > 2.0 with 10–20 triggers) are almost certainly noise — ignore.
- Win rates look high (65–78% on over_1.5 / over_2.5) but make sense: by the time xG pressure is high mid-game, the match is already goal-heavy.

### Config at time of last run

```python
BETFAIR_COMMISSION = 0.05   # 5% on net winnings
ODDS_HAIRCUT       = 0.92   # 8% conservative in-play haircut
MIN_EV             = 0.05
MIN_TRIGGERS       = 50
```

---

## Caveats

1. **All in-sample.** Every match with odds (2015/16) was also used to tune thresholds. EV is likely overstated. Need at minimum a different season for out-of-sample validation.

2. **Small odds coverage.** Only ~27% of signals (1,283 / 4,191) have odds attached. Unpriced matches bias the win-rate/EV upward if they're systematically different (e.g. only high-liquidity games get matched).

3. **StatsBomb xG ≠ FotMob xG.** The live pipeline would use FotMob. StatsBomb uses a different model — win rates/thresholds are not directly transferable without re-calibrating on FotMob data.

4. **Over-3.5 market is thin.** High avg odds (3–6x) mean the Betfair market has low liquidity in-play. Getting full stake matched at trigger time is not guaranteed. Treat profit figures as theoretical.

5. **Hold-to-FT win model.** Backtest assumes holding the bet to full time. In practice you'd often trade out. Actual realised P&L will differ.

---

## Next Steps (priority order)

### 1. Out-of-sample validation (blocker for going live)

**Option A — Understat adapter (recommended, fast):**  
Build `scrapers/understat.py` using the `understat` Python package. Has shot-level xG for EPL, La Liga, Bundesliga, Serie A, Ligue 1, RFPL from 2014/15 → 2024/25.  
- Train on 2015/16–2019/20, test on 2020/21–2022/23, hold out 2023/24 for walk-forward validation.
- Note: Understat xG model ≠ FotMob; findings are indicative, not directly deployable.

**Option B — Run FotMob scraper locally:**  
The `run.py ingest` pipeline works on a residential IP (no 403s). Run it on your machine, zip `data/raw_json/` and upload here (or push to GitHub). Then set `SEASONS = ["2019/2020", "2020/2021", "2021/2022"]` for a proper in/out split.

**Option C — Download more Betfair historical seasons:**  
Get 2019 and 2020 archives from historicdata.betfair.com to pair with StatsBomb 2015/16 in a time-split test (Betfair odds train on first half of season, test on second half). Weaker split but no new xG data needed.

### 2. Match Betfair data to correct seasons

Upload Betfair 2015 and 2016 archives (not just 2021) so odds align with the 2015/16 StatsBomb matches cleanly. The current 2021 data matches ~27% of signals — getting 2015/16 Betfair files would cover the other 73%.

### 3. Kelly sizing analysis

For the top 3 strategies, compute optimal Kelly fraction (`f* = (p*(b+1) - 1) / b`) and simulate bankroll growth vs ruin risk. This is the last gate before Phase 2.

### 4. Phase 2: Live monitor (only after step 1 validates positive EV)

Files to build:
- `live/monitor.py` — polls `GET /api/matches?date={YYYYMMDD}` every ~60s, calls `parse_match`, runs signal detection in real-time
- `live/alerts.py` — formats Telegram message with Kelly stake recommendation, sends via bot

Config already has `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `BETFAIR_APP_KEY` placeholder fields.

---

## Running the Pipeline

```bash
# Install
pip install -r requirements.txt
cp config.example.py config.py   # edit league/season scope as needed

# Full pipeline
python run.py all                 # init-db → ingest → signals → backtest → ev

# Individual steps
python run.py init-db
python run.py ingest              # FotMob (run locally) or statsbomb (runs on server)
python run.py signals
python run.py backtest
python run.py ev                  # no-odds mode if data/betfair_historical/ is empty

# Tests
pytest -q
```

### To add Betfair data

Drop `.bz2` streaming JSON files into `data/betfair_historical/` (any subdirectory structure is fine — `list_files()` recurses). Re-run `python run.py ev` — odds are recomputed from scratch each time.

### To ingest StatsBomb data directly

```python
from scrapers.statsbomb import COMPETITIONS, ingest_competition
from db.connection import get_conn, init_db
conn = get_conn(); init_db(conn)
ingest_competition(comp_id=2, season_id=27, conn=conn)  # PL 2015/16
```

See `scrapers/statsbomb.py` for the full `COMPETITIONS` dict.

---

## File Locations

| Path | Contents |
|---|---|
| `data/db/xg_signals.db` | Main SQLite DB (gitignored) |
| `data/raw_json/sb/` | StatsBomb cached match JSONs |
| `data/betfair_historical/2021/` | Betfair streaming JSON files (uploaded) |
| `results/signal_ev_table.csv` | Full 288-row grid search results |
| `results/best_signals.csv` | 123 rows: EV > 0.05 and n ≥ 50 |
| `CLAUDE.md` | Full project brief + implementation decisions |
