"""StatsBomb open-data adapter.

Maps StatsBomb's free event data into the existing matches + shots schema so the
full signals → backtest → ev pipeline can run without any FotMob access.

Data source: https://github.com/statsbomb/open-data (no login required).
Competition IDs: La Liga = 11, Champions League = 16, etc. Full list via
  get_competitions() or `python -m scrapers.statsbomb` which prints them.

Caching: event files are saved to data/raw_json/sb/{match_id}.json and match
lists to data/raw_json/sb/matches_{comp_id}_{season_id}.json so re-runs are
instant from disk.
"""

import json
import os
import time

import requests

import config
from db.connection import get_conn, init_db
from utils.logging import get_logger

logger = get_logger("statsbomb")

_BASE = "https://raw.githubusercontent.com/statsbomb/open-data/master/data"
_CACHE = os.path.join(config.RAW_CACHE_DIR, "sb")
_DELAY = 0.2  # seconds between GitHub requests (polite)

# StatsBomb shot outcome names that count as a goal (exclude own goals from
# the opposing team — own-goal events appear as 'Shot' on the scoring side).
_GOAL_OUTCOMES = {"goal"}
# Outcome → FotMob-style shot_type label
_OUTCOME_MAP = {
    "Goal": "Goal",
    "Saved": "SavedShot",
    "Blocked": "BlockedShot",
    "Off T": "Miss",
    "Wayward": "Miss",
    "Post": "Miss",
    "Saved Off Target": "SavedShot",
    "Saved to Post": "SavedShot",
    "No Touch": "Miss",
}

_session = requests.Session()
_session.headers["User-Agent"] = "sportbet-backtester/1.0"


def _cached_fetch(url: str, cache_path: str) -> dict | list:
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    resp = _session.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    time.sleep(_DELAY)
    return data


def get_competitions() -> list[dict]:
    return _cached_fetch(
        f"{_BASE}/competitions.json",
        os.path.join(_CACHE, "competitions.json"),
    )


def get_matches(competition_id: int, season_id: int) -> list[dict]:
    return _cached_fetch(
        f"{_BASE}/matches/{competition_id}/{season_id}.json",
        os.path.join(_CACHE, f"matches_{competition_id}_{season_id}.json"),
    )


def get_events(match_id: int) -> list[dict]:
    return _cached_fetch(
        f"{_BASE}/events/{match_id}.json",
        os.path.join(_CACHE, f"{match_id}.json"),
    )


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _make_match_id(sb_id: int) -> str:
    """Prefix with 'sb' so IDs don't collide with future FotMob entries."""
    return f"sb{sb_id}"


def parse_match(match: dict, events: list[dict]) -> tuple[dict, list[dict]]:
    """Convert StatsBomb match + events → (match_row, shot_rows)."""
    sb_id = match["match_id"]
    match_id = _make_match_id(sb_id)
    home_team = match["home_team"]["home_team_name"]
    away_team = match["away_team"]["away_team_name"]

    meta = {
        "match_id": match_id,
        "date": match.get("match_date"),
        "league": match.get("competition", {}).get("competition_name"),
        "season": match.get("season", {}).get("season_name"),
        "home_team": home_team,
        "away_team": away_team,
        "home_goals": match.get("home_score"),
        "away_goals": match.get("away_score"),
        "total_goals": (match.get("home_score") or 0) + (match.get("away_score") or 0),
    }

    shots = [e for e in events if e.get("type", {}).get("name") == "Shot"]
    if not shots:
        return meta, []

    shots_sorted = sorted(shots, key=lambda e: (e.get("minute", 0), e.get("index", 0)))

    cum_h = cum_a = 0.0
    score_h = score_a = 0
    rows: list[dict] = []
    for evt in shots_sorted:
        is_home = evt.get("team", {}).get("name") == home_team
        shot_info = evt.get("shot") or {}
        xg = float(shot_info.get("statsbomb_xg") or 0.0)
        outcome = shot_info.get("outcome", {}).get("name", "")
        body_part = shot_info.get("body_part", {}).get("name", "")
        is_goal = outcome.lower() == "goal"

        if is_home:
            cum_h += xg
        else:
            cum_a += xg
        if is_goal:
            if is_home:
                score_h += 1
            else:
                score_a += 1

        rows.append({
            "match_id": match_id,
            "minute": evt.get("minute", 0),
            "added_time": 0,
            "team": "home" if is_home else "away",
            "player": evt.get("player", {}).get("name"),
            "xg": xg,
            "is_goal": 1 if is_goal else 0,
            "shot_type": body_part or _OUTCOME_MAP.get(outcome, "Miss"),
            "cumulative_xg_home": round(cum_h, 6),
            "cumulative_xg_away": round(cum_a, 6),
            "cumulative_xg_total": round(cum_h + cum_a, 6),
            "score_home": score_h,
            "score_away": score_a,
        })
    return meta, rows


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #
_MATCH_COLS = (
    "match_id", "date", "league", "season",
    "home_team", "away_team", "home_goals", "away_goals", "total_goals",
)
_SHOT_COLS = (
    "match_id", "minute", "added_time", "team", "player", "xg", "is_goal",
    "shot_type", "cumulative_xg_home", "cumulative_xg_away", "cumulative_xg_total",
    "score_home", "score_away",
)


def ingest_competition(competition_id: int, season_id: int, conn, *, limit: int | None = None) -> dict:
    matches = get_matches(competition_id, season_id)
    if limit:
        matches = matches[:limit]

    inserted = skipped = shots_total = 0
    for match in matches:
        sb_id = match["match_id"]
        match_id = _make_match_id(sb_id)
        try:
            events = get_events(sb_id)
            meta, shot_rows = parse_match(match, events)
        except Exception as exc:
            logger.warning("Match %s failed: %s", sb_id, exc)
            skipped += 1
            continue

        if not shot_rows:
            logger.warning("Match %s (%s v %s) has no shots; skipping",
                           match_id, match["home_team"]["home_team_name"],
                           match["away_team"]["away_team_name"])
            skipped += 1
            continue

        ph = ", ".join("?" for _ in _MATCH_COLS)
        conn.execute(
            f"INSERT OR REPLACE INTO matches ({', '.join(_MATCH_COLS)}) VALUES ({ph})",
            [meta.get(c) for c in _MATCH_COLS],
        )
        conn.execute("DELETE FROM shots WHERE match_id = ?", (match_id,))
        conn.executemany(
            f"INSERT INTO shots ({', '.join(_SHOT_COLS)}) VALUES ({', '.join('?' for _ in _SHOT_COLS)})",
            [[r.get(c) for c in _SHOT_COLS] for r in shot_rows],
        )
        conn.commit()
        inserted += 1
        shots_total += len(shot_rows)

    logger.info(
        "comp=%s season=%s: %d inserted, %d skipped, %d shots",
        competition_id, season_id, inserted, skipped, shots_total,
    )
    return {"competition_id": competition_id, "season_id": season_id,
            "inserted": inserted, "skipped": skipped, "shots": shots_total}


def ingest_statsbomb(competition_name: str = "La Liga", conn=None) -> list[dict]:
    """Ingest all seasons of the named competition."""
    own = conn is None
    if own:
        conn = get_conn()
        init_db(conn)
    try:
        competitions = get_competitions()
        selected = [
            (c["competition_id"], c["season_id"], c["season_name"])
            for c in competitions
            if c["competition_name"] == competition_name
        ]
        logger.info("Ingesting %d %s seasons", len(selected), competition_name)
        summaries = []
        for cid, sid, sname in selected:
            logger.info("  Season: %s", sname)
            summaries.append(ingest_competition(cid, sid, conn))
        total_m = sum(s["inserted"] for s in summaries)
        total_s = sum(s["shots"] for s in summaries)
        logger.info("DONE: %d matches, %d shots total", total_m, total_s)
        return summaries
    finally:
        if own:
            conn.close()


if __name__ == "__main__":  # pragma: no cover
    import sys
    name = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "La Liga"
    comps = get_competitions()
    names = sorted(set(c["competition_name"] for c in comps))
    print("Available competitions:", names)
    print(f"\nSelected: {name!r}")
    ingest_statsbomb(name)
