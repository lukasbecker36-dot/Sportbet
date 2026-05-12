"""Orchestrates FotMob scraping -> SQLite (matches + shots tables)."""

import sqlite3

import config
from db.connection import get_conn, init_db
from scrapers import fotmob
from utils.logging import get_logger

logger = get_logger("ingest")

_MATCH_COLS = (
    "match_id", "date", "league", "season",
    "home_team", "away_team", "home_goals", "away_goals", "total_goals",
)
_SHOT_COLS = (
    "match_id", "minute", "added_time", "team", "player", "xg", "is_goal",
    "shot_type", "cumulative_xg_home", "cumulative_xg_away", "cumulative_xg_total",
    "score_home", "score_away",
)


def _insert_match(conn: sqlite3.Connection, meta: dict) -> None:
    placeholders = ", ".join("?" for _ in _MATCH_COLS)
    conn.execute(
        f"INSERT OR REPLACE INTO matches ({', '.join(_MATCH_COLS)}) VALUES ({placeholders})",
        [meta.get(c) for c in _MATCH_COLS],
    )


def _replace_shots(conn: sqlite3.Connection, match_id: str, rows: list[dict]) -> None:
    conn.execute("DELETE FROM shots WHERE match_id = ?", (match_id,))
    if not rows:
        return
    placeholders = ", ".join("?" for _ in _SHOT_COLS)
    conn.executemany(
        f"INSERT INTO shots ({', '.join(_SHOT_COLS)}) VALUES ({placeholders})",
        [[r.get(c) for c in _SHOT_COLS] for r in rows],
    )


def ingest_league_season(league_id: int, season: str, conn: sqlite3.Connection) -> dict:
    match_ids = fotmob.get_league_matches(league_id, season)
    inserted = skipped = shot_count = 0
    for mid in match_ids:
        try:
            detail = fotmob.get_match_details(mid)
            meta, shots = fotmob.parse_match(detail, mid)
        except Exception as exc:  # noqa: BLE001 - one bad match shouldn't kill the run
            logger.warning("Match %s failed to fetch/parse: %s", mid, exc)
            skipped += 1
            continue
        if not shots:
            logger.warning("Match %s has no shotmap; skipping", mid)
            skipped += 1
            continue
        _insert_match(conn, meta)
        _replace_shots(conn, mid, shots)
        conn.commit()
        inserted += 1
        shot_count += len(shots)
    logger.info(
        "League %s season %s: %d inserted, %d skipped, %d shots",
        league_id, season, inserted, skipped, shot_count,
    )
    return {"league": league_id, "season": season, "inserted": inserted,
            "skipped": skipped, "shots": shot_count}


def ingest_all(conn: sqlite3.Connection | None = None) -> list[dict]:
    own = conn is None
    if own:
        conn = get_conn()
        init_db(conn)
    try:
        summaries = []
        for league_id in config.LEAGUE_IDS:
            for season in config.SEASONS:
                summaries.append(ingest_league_season(league_id, season, conn))
        total_in = sum(s["inserted"] for s in summaries)
        total_skip = sum(s["skipped"] for s in summaries)
        total_shots = sum(s["shots"] for s in summaries)
        logger.info("INGEST DONE: %d matches, %d skipped, %d shots", total_in, total_skip, total_shots)
        return summaries
    finally:
        if own:
            conn.close()


if __name__ == "__main__":  # pragma: no cover
    ingest_all()
