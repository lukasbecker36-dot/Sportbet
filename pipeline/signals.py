"""Minute-by-minute xG feature frames and signal detection.

A "signal" fires the minute the rolling-xG rate transitions from below the
lowest configured threshold to at-or-above it (a *crossing event*). At most a
handful per match, which keeps samples roughly independent. The backtest then
re-applies higher thresholds and min/max-minute windows by filtering on the
stored `signal_value` and `trigger_minute`.
"""

import sqlite3

import numpy as np
import pandas as pd

import config
from db.connection import get_conn
from utils.logging import get_logger

logger = get_logger("signals")

PRIMARY_SIGNAL = "xg_rate_15m"
_WINDOWS = (10, 15, 20)
_GOAL_HORIZONS = (10, 15, 20, 30)
_SIGNAL_COLS = (
    "match_id", "trigger_minute", "signal_type", "signal_value",
    "score_at_trigger", "goals_total_at_trigger",
    "next_goal_within_10", "next_goal_within_15",
    "next_goal_within_20", "next_goal_within_30",
    "betfair_over05_odds", "betfair_over15_odds",
    "betfair_over25_odds", "betfair_over35_odds",
)


def _load_shots(match_id: str, conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT minute, team, xg, is_goal FROM shots WHERE match_id = ? ORDER BY minute",
        conn, params=(match_id,),
    )


def build_match_frame(match_id: str, conn: sqlite3.Connection) -> pd.DataFrame:
    """One row per minute (0..max(90, last shot)). Returns empty frame if no shots."""
    shots = _load_shots(match_id, conn)
    if shots.empty:
        return pd.DataFrame()

    last_minute = int(shots["minute"].max())
    end = max(90, last_minute)
    idx = pd.RangeIndex(0, end + 1, name="minute")

    xg_home_min = shots.loc[shots.team == "home"].groupby("minute")["xg"].sum().reindex(idx, fill_value=0.0)
    xg_away_min = shots.loc[shots.team == "away"].groupby("minute")["xg"].sum().reindex(idx, fill_value=0.0)
    xg_total_min = (xg_home_min + xg_away_min)
    goals_min = shots.groupby("minute")["is_goal"].sum().reindex(idx, fill_value=0).astype(int)
    goals_home_min = shots.loc[shots.team == "home"].groupby("minute")["is_goal"].sum().reindex(idx, fill_value=0).astype(int)
    goals_away_min = shots.loc[shots.team == "away"].groupby("minute")["is_goal"].sum().reindex(idx, fill_value=0).astype(int)

    df = pd.DataFrame(index=idx)
    df["cumulative_xg_home"] = xg_home_min.cumsum()
    df["cumulative_xg_away"] = xg_away_min.cumsum()
    df["cumulative_xg_total"] = df["cumulative_xg_home"] + df["cumulative_xg_away"]
    df["goals_home"] = goals_home_min.cumsum()
    df["goals_away"] = goals_away_min.cumsum()
    df["goals_total"] = goals_min.cumsum()
    for w in _WINDOWS:
        df[f"xg_rate_{w}m"] = xg_total_min.rolling(window=w, min_periods=1).sum()
    df["xg_deficit"] = (df["cumulative_xg_home"] - df["cumulative_xg_away"]).abs()
    df["minutes_remaining"] = np.clip(90 - np.arange(len(df)), 0, None)
    df["xg_per_goal"] = df["cumulative_xg_total"] / df["goals_total"].clip(lower=1)
    return df


def _goal_minutes(match_id: str, conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        "SELECT minute FROM shots WHERE match_id = ? AND is_goal = 1 ORDER BY minute",
        (match_id,),
    ).fetchall()
    return [int(r[0]) for r in rows]


def detect_signals(match_id: str, conn: sqlite3.Connection) -> list[dict]:
    df = build_match_frame(match_id, conn)
    if df.empty:
        return []
    lowest = min(config.XG_RATE_THRESHOLDS)
    floor_min = min(config.MIN_MINUTE)
    ceil_min = max(config.MAX_MINUTE)
    goal_mins = _goal_minutes(match_id, conn)

    series = df[PRIMARY_SIGNAL]
    above = series >= lowest
    rows: list[dict] = []
    prev_above = False
    for minute, is_above in above.items():
        if minute < floor_min or minute > ceil_min:
            prev_above = bool(is_above)
            continue
        if is_above and not prev_above:  # crossing event
            row = df.loc[minute]
            gh, ga = int(row["goals_home"]), int(row["goals_away"])
            gt = int(row["goals_total"])
            rec = {
                "match_id": str(match_id),
                "trigger_minute": int(minute),
                "signal_type": PRIMARY_SIGNAL,
                "signal_value": float(round(series.loc[minute], 6)),
                "score_at_trigger": f"{gh}-{ga}",
                "goals_total_at_trigger": gt,
                "betfair_over05_odds": None,
                "betfair_over15_odds": None,
                "betfair_over25_odds": None,
                "betfair_over35_odds": None,
            }
            for h in _GOAL_HORIZONS:
                later = any(minute < gm <= minute + h for gm in goal_mins)
                rec[f"next_goal_within_{h}"] = 1 if later else 0
            rows.append(rec)
        prev_above = bool(is_above)
    return rows


def populate_signals(conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    if own:
        conn = get_conn()
    try:
        conn.execute("DELETE FROM signals")
        match_ids = [r[0] for r in conn.execute("SELECT match_id FROM matches").fetchall()]
        placeholders = ", ".join("?" for _ in _SIGNAL_COLS)
        sql = f"INSERT INTO signals ({', '.join(_SIGNAL_COLS)}) VALUES ({placeholders})"
        total = 0
        for mid in match_ids:
            recs = detect_signals(mid, conn)
            if recs:
                conn.executemany(sql, [[r.get(c) for c in _SIGNAL_COLS] for r in recs])
                total += len(recs)
        conn.commit()
        logger.info("Populated %d signal rows across %d matches", total, len(match_ids))
        return total
    finally:
        if own:
            conn.close()


if __name__ == "__main__":  # pragma: no cover
    populate_signals()
