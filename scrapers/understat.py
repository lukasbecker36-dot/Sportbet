"""Understat adapter via the ``soccerdata`` library.

Maps Understat's shot-level xG into the existing ``matches`` + ``shots`` schema
so the rest of the pipeline (signals → backtest → ev) runs without changes.

``soccerdata`` does all the actual scraping + on-disk caching for us — re-runs
are instant once the local cache is warm.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Iterable

import pandas as pd

import soccerdata as sd

import config
from db.connection import get_conn, init_db
from utils.logging import get_logger

logger = get_logger("understat")

_MATCH_COLS = (
    "match_id", "date", "league", "season",
    "home_team", "away_team", "home_goals", "away_goals", "total_goals",
)
_SHOT_COLS = (
    "match_id", "minute", "added_time", "team", "player", "xg", "is_goal",
    "shot_type", "cumulative_xg_home", "cumulative_xg_away", "cumulative_xg_total",
    "score_home", "score_away",
)

# Result strings that count as a goal scored by the shooting team's *opponent*.
_OWN_GOAL = "Own Goal"
_GOAL = "Goal"


def _make_match_id(game_id) -> str:
    return f"us{int(game_id)}"


def _quiet_soccerdata():
    # soccerdata is chatty at INFO. Silence everything below WARNING.
    for name in ("soccerdata", "soccerdata.Understat", "TLSLibrary"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _patch_soccerdata_for_list_rosters() -> None:
    """soccerdata 1.x crashes on some matches (incl. parts of Bundesliga 24/25)
    where the JSON ``rosters["h"]`` is a list, not a dict. Wrap _read_match so
    it handles both shapes — without the patch the entire league's shot pull
    aborts on the first bad match.
    """
    import json as _json
    from pathlib import Path as _Path

    original_read = sd.Understat._read_match

    def _team_id(side):
        if isinstance(side, dict):
            return next(iter(side.values()))["team_id"]
        if isinstance(side, list) and side:
            return side[0]["team_id"]
        return None

    def patched(self, url: str, match_id: int):
        from soccerdata.understat import UNDERSTAT_URL  # local import to match original
        self._ensure_cookies()
        try:
            api_url = UNDERSTAT_URL + f"/getMatchData/{match_id}"
            filepath = self.data_dir / f"match_{match_id}.json"
            reader = self._request_api(api_url, filepath)
            data = _json.load(reader)

            home_team_name = self._extract_team_name(data["tmpl"]["home"])
            away_team_name = self._extract_team_name(data["tmpl"]["away"])
            rosters = data["rosters"]
            # Normalise list-shaped rosters to dict-shaped, so all downstream
            # iteration in read_shot_events that does ``team_data.values()``
            # still works without further patching.
            for side in ("h", "a"):
                side_roster = rosters.get(side)
                if isinstance(side_roster, list):
                    rosters[side] = {
                        str(p.get("id", i)): p for i, p in enumerate(side_roster)
                    }
            home_team_id = _team_id(rosters.get("h"))
            away_team_id = _team_id(rosters.get("a"))

            match_info = {
                "h": home_team_id, "a": away_team_id,
                "team_h": home_team_name, "team_a": away_team_name,
            }
            return {
                "match_info": match_info,
                "rostersData": rosters,
                "shotsData": data.get("shots") or {"h": [], "a": []},
            }
        except ConnectionError:
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning("Understat match %s skipped: %s", match_id, e)
            return None

    sd.Understat._read_match = patched
    logger.info("soccerdata._read_match patched for list-shaped rosters")


_patch_soccerdata_for_list_rosters()


def _build_rows_for_match(
    match_id: str,
    home_team: str,
    away_team: str,
    shots_df: pd.DataFrame,
) -> list[dict]:
    """Convert one game's shots into ordered shot rows with cumulative xG/score.

    Own-goal handling: the shot stays attributed to the shooter (so xG accrues
    correctly), but ``is_goal`` flips to the opposing side by emitting a
    zero-xG goal row for that team at the same minute. This mirrors what the
    existing pipeline expects (``team`` indexes both xG accumulation and goal
    counting in ``signals.build_match_frame``).
    """
    rows: list[dict] = []
    if shots_df.empty:
        return rows

    df = shots_df.sort_values("minute").reset_index()
    cum_h = cum_a = 0.0
    score_h = score_a = 0

    for _, s in df.iterrows():
        shooter_team = s["team"]
        is_home_shot = shooter_team == home_team
        is_away_shot = shooter_team == away_team
        if not (is_home_shot or is_away_shot):
            continue  # shouldn't happen — shooter not in match

        xg = float(s["xg"] or 0.0)
        minute = int(s["minute"]) if pd.notna(s["minute"]) else 0
        result = str(s["result"])

        # Attribute the xG to the side that took the shot.
        if is_home_shot:
            cum_h += xg
        else:
            cum_a += xg

        is_normal_goal = result == _GOAL
        is_own_goal = result == _OWN_GOAL

        # Update scoreline: own goals go to the opposing side.
        if is_normal_goal:
            if is_home_shot:
                score_h += 1
            else:
                score_a += 1
        elif is_own_goal:
            if is_home_shot:
                score_a += 1
            else:
                score_h += 1

        rows.append(
            {
                "match_id": match_id,
                "minute": minute,
                "added_time": 0,
                # For own goals: flip ``team`` so signals.py credits the right
                # side when grouping is_goal by team. xG still accrued above to
                # the shooter via cum_h/cum_a so cumulative columns are accurate.
                "team": (
                    "home" if (is_home_shot and not is_own_goal) or (is_away_shot and is_own_goal)
                    else "away"
                ),
                "player": s.get("player"),
                "xg": xg if not is_own_goal else 0.0,  # zero xg on the goal row
                "is_goal": 1 if (is_normal_goal or is_own_goal) else 0,
                "shot_type": (s.get("body_part") if pd.notna(s.get("body_part")) else None) or result,
                "cumulative_xg_home": round(cum_h, 6),
                "cumulative_xg_away": round(cum_a, 6),
                "cumulative_xg_total": round(cum_h + cum_a, 6),
                "score_home": score_h,
                "score_away": score_a,
            }
        )
    return rows


def _meta_from_schedule_row(
    row: pd.Series, match_id: str, season_label: str, league: str,
) -> dict:
    hg, ag = row.get("home_goals"), row.get("away_goals")
    return {
        "match_id": match_id,
        "date": row["date"].isoformat() if pd.notna(row.get("date")) else None,
        "league": league,
        "season": season_label,
        "home_team": row["home_team"],
        "away_team": row["away_team"],
        "home_goals": int(hg) if pd.notna(hg) else None,
        "away_goals": int(ag) if pd.notna(ag) else None,
        "total_goals": (
            int(hg) + int(ag) if pd.notna(hg) and pd.notna(ag) else None
        ),
    }


def _insert(conn: sqlite3.Connection, meta: dict, shots: list[dict]) -> None:
    ph_m = ", ".join("?" for _ in _MATCH_COLS)
    conn.execute(
        f"INSERT OR REPLACE INTO matches ({', '.join(_MATCH_COLS)}) VALUES ({ph_m})",
        [meta.get(c) for c in _MATCH_COLS],
    )
    conn.execute("DELETE FROM shots WHERE match_id = ?", (meta["match_id"],))
    if not shots:
        return
    ph_s = ", ".join("?" for _ in _SHOT_COLS)
    conn.executemany(
        f"INSERT INTO shots ({', '.join(_SHOT_COLS)}) VALUES ({ph_s})",
        [[r.get(c) for c in _SHOT_COLS] for r in shots],
    )


def ingest_understat(
    league: str = "ENG-Premier League",
    seasons: Iterable[str] = ("2025/2026",),
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Fetch & insert all matches for the named season(s)."""
    _quiet_soccerdata()
    own = conn is None
    if own:
        conn = get_conn()
        init_db(conn)
    try:
        seasons = list(seasons)
        season_label = seasons[0] if len(seasons) == 1 else ",".join(seasons)
        us = sd.Understat(leagues=league, seasons=seasons)
        schedule = us.read_schedule().reset_index()
        shots = us.read_shot_events().reset_index()
        # Restrict to completed matches with shot data.
        completed = schedule[schedule["is_result"] & schedule["has_data"]]
        logger.info(
            "Understat %s %s: %d matches in schedule, %d completed, %d total shots",
            league, season_label, len(schedule), len(completed), len(shots),
        )

        shots_by_game = dict(tuple(shots.groupby("game_id")))

        inserted = skipped = shots_total = 0
        for _, row in completed.iterrows():
            gid = row["game_id"]
            match_id = _make_match_id(gid)
            game_shots = shots_by_game.get(gid)
            if game_shots is None or game_shots.empty:
                logger.warning("Match %s (%s v %s) has no shot data; skipping",
                               match_id, row["home_team"], row["away_team"])
                skipped += 1
                continue
            meta = _meta_from_schedule_row(row, match_id, season_label, league)
            shot_rows = _build_rows_for_match(
                match_id, row["home_team"], row["away_team"], game_shots,
            )
            _insert(conn, meta, shot_rows)
            inserted += 1
            shots_total += len(shot_rows)
        conn.commit()

        logger.info("DONE: %d inserted, %d skipped, %d shots", inserted, skipped, shots_total)
        return {"league": league, "season": season_label,
                "inserted": inserted, "skipped": skipped, "shots": shots_total}
    finally:
        if own:
            conn.close()


if __name__ == "__main__":  # pragma: no cover
    print(ingest_understat())
