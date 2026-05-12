"""Signal feature frame + crossing-event detection on synthetic shots."""

import os

import pytest

from db.connection import get_conn, init_db
import pipeline.signals as signals_mod


def _make_db(tmp_path, shots):
    db_path = os.path.join(tmp_path, "t.db")
    conn = get_conn(db_path)
    init_db(conn)
    conn.execute(
        "INSERT INTO matches (match_id, date, league, season, home_team, away_team, "
        "home_goals, away_goals, total_goals) VALUES ('M1','2024-01-01','L','2023/2024','H','A',?,?,?)",
        (sum(1 for s in shots if s[3] == 'home' and s[5]),
         sum(1 for s in shots if s[3] == 'away' and s[5]),
         sum(1 for s in shots if s[5])),
    )
    # shots tuples: (minute, added, team, _, xg, is_goal)
    cum_h = cum_a = sh = sa = 0.0
    for minute, added, team, _t, xg, is_goal in shots:
        if team == 'home':
            cum_h += xg
        else:
            cum_a += xg
        if is_goal:
            if team == 'home':
                sh += 1
            else:
                sa += 1
        conn.execute(
            "INSERT INTO shots (match_id, minute, added_time, team, player, xg, is_goal, shot_type, "
            "cumulative_xg_home, cumulative_xg_away, cumulative_xg_total, score_home, score_away) "
            "VALUES ('M1',?,?,?,'p',?,?,?,?,?,?,?,?)",
            (minute, added, team, xg, 1 if is_goal else 0, 'foot', cum_h, cum_a, cum_h + cum_a, int(sh), int(sa)),
        )
    conn.commit()
    return conn


def test_xg_rate_window_values(tmp_path):
    shots = [
        (50, 0, 'home', None, 0.1, False),
        (52, 0, 'home', None, 0.1, False),
        (54, 0, 'home', None, 0.1, False),
    ]
    conn = _make_db(tmp_path, shots)
    df = signals_mod.build_match_frame("M1", conn)
    assert df.loc[54, "xg_rate_15m"] == pytest.approx(0.3)
    assert df.loc[55, "xg_rate_15m"] == pytest.approx(0.3)
    assert df.loc[70, "xg_rate_15m"] == pytest.approx(0.0)
    assert df.loc[54, "cumulative_xg_total"] == pytest.approx(0.3)
    assert df.loc[54, "minutes_remaining"] == 90 - 54


def test_crossing_event_detected_once(tmp_path):
    # Goal at minute 60 lets us check the next-goal horizons.
    shots = [
        (50, 0, 'home', None, 0.1, False),
        (52, 0, 'home', None, 0.1, False),
        (54, 0, 'home', None, 0.1, False),
        (60, 0, 'home', None, 0.4, True),
    ]
    conn = _make_db(tmp_path, shots)
    recs = signals_mod.detect_signals("M1", conn)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["trigger_minute"] == 52
    assert rec["signal_value"] == pytest.approx(0.2)
    assert rec["score_at_trigger"] == "0-0"
    assert rec["goals_total_at_trigger"] == 0
    assert rec["next_goal_within_10"] == 1   # goal at 60 <= 52+10? 62 -> yes
    assert rec["next_goal_within_15"] == 1
    assert rec["next_goal_within_30"] == 1


def test_no_signal_below_threshold(tmp_path):
    # Single 0.1 xG shot -> rate never reaches the lowest threshold (0.20).
    conn = _make_db(tmp_path, [(50, 0, 'home', None, 0.1, False)])
    assert signals_mod.detect_signals("M1", conn) == []


def test_no_signal_outside_minute_window(tmp_path):
    # Two 0.1 shots before the earliest MIN_MINUTE (30) -> rate crosses too early.
    conn = _make_db(tmp_path, [(10, 0, 'home', None, 0.1, False), (12, 0, 'home', None, 0.1, False)])
    assert signals_mod.detect_signals("M1", conn) == []
