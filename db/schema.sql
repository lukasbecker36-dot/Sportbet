CREATE TABLE IF NOT EXISTS matches (
    match_id        TEXT PRIMARY KEY,
    date            TEXT,
    league          TEXT,
    season          TEXT,
    home_team       TEXT,
    away_team       TEXT,
    home_goals      INTEGER,
    away_goals      INTEGER,
    total_goals     INTEGER
);

CREATE TABLE IF NOT EXISTS shots (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id                TEXT,
    minute                  INTEGER,
    added_time              INTEGER DEFAULT 0,
    team                    TEXT,       -- 'home' or 'away'
    player                  TEXT,
    xg                      REAL,
    is_goal                 INTEGER,    -- 0 or 1
    shot_type               TEXT,       -- header, foot etc
    cumulative_xg_home      REAL,
    cumulative_xg_away      REAL,
    cumulative_xg_total     REAL,
    score_home              INTEGER,
    score_away              INTEGER,
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE TABLE IF NOT EXISTS signals (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id                TEXT,
    trigger_minute          INTEGER,
    signal_type             TEXT,       -- e.g. 'xg_rate_15m', 'cumulative_xg'
    signal_value            REAL,
    score_at_trigger        TEXT,       -- e.g. '1-0'
    goals_total_at_trigger  INTEGER,
    next_goal_within_10     INTEGER,    -- 0 or 1
    next_goal_within_15     INTEGER,
    next_goal_within_20     INTEGER,
    next_goal_within_30     INTEGER,
    betfair_over05_odds     REAL,
    betfair_over15_odds     REAL,
    betfair_over25_odds     REAL,
    betfair_over35_odds     REAL,
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE INDEX IF NOT EXISTS idx_shots_match_minute ON shots(match_id, minute);
CREATE INDEX IF NOT EXISTS idx_signals_match_minute ON signals(match_id, trigger_minute);
CREATE INDEX IF NOT EXISTS idx_signals_type ON signals(signal_type);
