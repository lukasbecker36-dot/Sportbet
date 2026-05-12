"""FotMob internal-API wrapper: league index, match details, shotmap parsing.

All raw responses are cached to disk (config.RAW_CACHE_DIR) before parsing so the
full dataset can be re-parsed without re-scraping.
"""

import json
import os
import random
import time

import requests

import config
from utils.logging import get_logger

logger = get_logger("fotmob")

BASE = "https://www.fotmob.com/api"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.fotmob.com/",
    "Origin": "https://www.fotmob.com",
}

_session = requests.Session()
_session.headers.update(HEADERS)

_GOAL_EVENT_TYPES = {"goal"}  # 'Goal'; own-goals handled via the isOwnGoal flag


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _get_json(url: str) -> dict:
    """GET with one 60s backoff retry on 429/403. Logs every request."""
    resp = _session.get(url, timeout=30)
    logger.info("GET %s -> %s", url, resp.status_code)
    if resp.status_code in (429, 403):
        logger.warning("Rate limited (%s); backing off %.0fs", resp.status_code, config.BACKOFF_SECONDS)
        _sleep(config.BACKOFF_SECONDS)
        resp = _session.get(url, timeout=30)
        logger.info("GET (retry) %s -> %s", url, resp.status_code)
    resp.raise_for_status()
    return resp.json()


def _cached_get(url: str, cache_path: str, post_delay: float) -> dict:
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    data = _get_json(url)
    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    if post_delay:
        _sleep(post_delay)
    return data


# --------------------------------------------------------------------------- #
# League index
# --------------------------------------------------------------------------- #
def _league_cache_path(league_id: int, season: str) -> str:
    safe_season = season.replace("/", "-")
    return os.path.join(config.RAW_CACHE_DIR, f"league_{league_id}_{safe_season}.json")


def _iter_match_dicts(league_json: dict):
    """Yield match dicts from the various shapes the leagues endpoint returns."""
    matches = league_json.get("matches")
    if isinstance(matches, dict):
        for key in ("allMatches", "all", "matches"):
            if isinstance(matches.get(key), list):
                yield from matches[key]
                return
    if isinstance(matches, list):
        yield from matches
        return
    for key in ("fixtures", "allMatches"):
        if isinstance(league_json.get(key), list):
            yield from league_json[key]
            return


def _is_finished(match: dict) -> bool:
    status = match.get("status") or {}
    if isinstance(status, dict):
        if status.get("finished") is True:
            return True
        if status.get("started") is False:
            return False
        reason = (status.get("reason") or {})
        short = str(reason.get("short", "")).upper() if isinstance(reason, dict) else ""
        if short in {"FT", "AET", "PEN"}:
            return True
        # Fall back to: has a score and not cancelled.
        if status.get("cancelled"):
            return False
    # If there's a recorded score string like "2 - 1", treat as finished.
    score = status.get("scoreStr") if isinstance(status, dict) else None
    return bool(score)


def get_league_matches(league_id: int, season: str) -> list[str]:
    """Return finished match ids for a league/season."""
    url = f"{BASE}/leagues?id={league_id}&season={season}"
    data = _cached_get(url, _league_cache_path(league_id, season), config.LEAGUE_INDEX_DELAY)
    ids: list[str] = []
    for match in _iter_match_dicts(data):
        mid = match.get("id") or match.get("matchId")
        if mid is None:
            continue
        if _is_finished(match):
            ids.append(str(mid))
    logger.info("League %s season %s: %d finished matches", league_id, season, len(ids))
    return ids


# --------------------------------------------------------------------------- #
# Match details
# --------------------------------------------------------------------------- #
def _match_cache_path(match_id: str) -> str:
    return os.path.join(config.RAW_CACHE_DIR, f"{match_id}.json")


def get_match_details(match_id: str) -> dict:
    url = f"{BASE}/matchDetails?matchId={match_id}"
    delay = random.uniform(config.SCRAPE_DELAY_MIN, config.SCRAPE_DELAY_MAX)
    return _cached_get(url, _match_cache_path(match_id), delay)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _general(detail: dict) -> dict:
    return detail.get("general") or {}


def _header_teams(detail: dict) -> list[dict]:
    header = detail.get("header") or {}
    teams = header.get("teams")
    if isinstance(teams, list):
        return teams
    return []


def _shotmap_shots(detail: dict) -> list[dict]:
    content = detail.get("content") or {}
    shotmap = content.get("shotmap") or {}
    shots = shotmap.get("shots")
    return shots if isinstance(shots, list) else []


def _team_ids(detail: dict) -> tuple[int | None, int | None]:
    gen = _general(detail)
    home = gen.get("homeTeam") or {}
    away = gen.get("awayTeam") or {}
    return home.get("id"), away.get("id")


def _final_score(detail: dict) -> tuple[int | None, int | None]:
    teams = _header_teams(detail)
    if len(teams) == 2:
        try:
            return int(teams[0].get("score")), int(teams[1].get("score"))
        except (TypeError, ValueError):
            pass
    return None, None


def _match_metadata(detail: dict, match_id: str) -> dict:
    gen = _general(detail)
    teams = _header_teams(detail)
    home_name = (gen.get("homeTeam") or {}).get("name")
    away_name = (gen.get("awayTeam") or {}).get("name")
    if not home_name and len(teams) == 2:
        home_name, away_name = teams[0].get("name"), teams[1].get("name")
    home_goals, away_goals = _final_score(detail)
    season = gen.get("parentLeagueSeason") or gen.get("leagueSeason")
    date = gen.get("matchTimeUTC") or gen.get("matchTimeUTCDate")
    total = (home_goals + away_goals) if (home_goals is not None and away_goals is not None) else None
    return {
        "match_id": str(match_id),
        "date": date,
        "league": gen.get("leagueName"),
        "season": season,
        "home_team": home_name,
        "away_team": away_name,
        "home_goals": home_goals,
        "away_goals": away_goals,
        "total_goals": total,
    }


def _shot_minute(shot: dict) -> tuple[int, int]:
    minute = shot.get("min")
    minute = int(minute) if minute is not None else 0
    added = shot.get("minAdded")
    added = int(added) if added is not None else 0
    return minute, added


def _is_home_shot(shot: dict, home_id: int | None) -> bool:
    if "isHomeTeam" in shot and shot["isHomeTeam"] is not None:
        return bool(shot["isHomeTeam"])
    team_id = shot.get("teamId")
    if team_id is not None and home_id is not None:
        return int(team_id) == int(home_id)
    # Last resort: assume away (caller still gets a usable, if imperfect, row).
    return False


def _is_goal(shot: dict) -> bool:
    event = str(shot.get("eventType", "")).strip().lower()
    return event in _GOAL_EVENT_TYPES


def parse_match(detail: dict, match_id: str | None = None):
    """Return (match_row: dict, shot_rows: list[dict]).

    shot_rows is empty when the match has no shotmap (cup ties, lower leagues).
    Cumulative xG and scoreline are reconstructed by iterating shots in minute
    order; for a goal shot, the recorded scoreline is the post-shot score.
    """
    if match_id is None:
        match_id = str(_general(detail).get("matchId") or "")
    meta = _match_metadata(detail, match_id)

    raw_shots = _shotmap_shots(detail)
    if not raw_shots:
        return meta, []

    home_id, _away_id = _team_ids(detail)
    ordered = sorted(raw_shots, key=_shot_minute)

    cum_home = cum_away = 0.0
    score_home = score_away = 0
    rows: list[dict] = []
    for shot in ordered:
        minute, added = _shot_minute(shot)
        is_home = _is_home_shot(shot, home_id)
        xg = shot.get("expectedGoals")
        xg = float(xg) if xg is not None else 0.0
        goal = _is_goal(shot)

        if is_home:
            cum_home += xg
        else:
            cum_away += xg
        if goal:
            # Own goals credit the opposing team's scoreline.
            scored_for_home = is_home != bool(shot.get("isOwnGoal"))
            if scored_for_home:
                score_home += 1
            else:
                score_away += 1

        rows.append(
            {
                "match_id": str(match_id),
                "minute": minute,
                "added_time": added,
                "team": "home" if is_home else "away",
                "player": shot.get("playerName") or shot.get("fullName"),
                "xg": xg,
                "is_goal": 1 if goal else 0,
                "shot_type": shot.get("shotType") or shot.get("eventType"),
                "cumulative_xg_home": round(cum_home, 6),
                "cumulative_xg_away": round(cum_away, 6),
                "cumulative_xg_total": round(cum_home + cum_away, 6),
                "score_home": score_home,
                "score_away": score_away,
            }
        )
    return meta, rows


# --------------------------------------------------------------------------- #
# Live helper (used by the deferred live/ phase; harmless to keep here)
# --------------------------------------------------------------------------- #
def get_matches_on_date(yyyymmdd: str) -> dict:
    url = f"{BASE}/matches?date={yyyymmdd}"
    return _get_json(url)


if __name__ == "__main__":  # pragma: no cover
    import sys

    mid = sys.argv[1] if len(sys.argv) > 1 else "4193843"
    detail = get_match_details(mid)
    meta, shots = parse_match(detail, mid)
    print("MATCH:", json.dumps(meta, indent=2))
    print(f"SHOTS: {len(shots)}")
    for s in shots[:10]:
        print(s)
