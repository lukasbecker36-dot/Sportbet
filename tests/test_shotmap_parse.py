"""parse_match: cumulative xG, scoreline reconstruction, no-shotmap handling."""

from scrapers.fotmob import parse_match

HOME_ID, AWAY_ID = 100, 200

SAMPLE = {
    "general": {
        "matchId": "999",
        "leagueName": "Test League",
        "parentLeagueSeason": "2023/2024",
        "matchTimeUTC": "2024-01-15T15:00:00Z",
        "homeTeam": {"id": HOME_ID, "name": "Alpha FC"},
        "awayTeam": {"id": AWAY_ID, "name": "Beta United"},
    },
    "header": {"teams": [{"name": "Alpha FC", "score": 2}, {"name": "Beta United", "score": 1}]},
    "content": {
        "shotmap": {
            "shots": [
                {"min": 10, "teamId": HOME_ID, "expectedGoals": 0.10, "eventType": "Miss",
                 "playerName": "A1", "shotType": "RightFoot"},
                {"min": 23, "teamId": AWAY_ID, "expectedGoals": 0.30, "eventType": "Goal",
                 "playerName": "B1", "shotType": "Header"},
                {"min": 40, "teamId": HOME_ID, "expectedGoals": 0.50, "eventType": "Goal",
                 "playerName": "A2", "shotType": "LeftFoot"},
                {"min": 77, "teamId": HOME_ID, "expectedGoals": 0.20, "eventType": "Goal",
                 "playerName": "A3", "shotType": "RightFoot"},
            ]
        }
    },
}


def test_metadata_and_score():
    meta, shots = parse_match(SAMPLE)
    assert meta["match_id"] == "999"
    assert meta["home_team"] == "Alpha FC"
    assert meta["away_team"] == "Beta United"
    assert meta["home_goals"] == 2 and meta["away_goals"] == 1
    assert meta["total_goals"] == 3
    assert len(shots) == 4


def test_cumulative_xg_and_running_score():
    _, shots = parse_match(SAMPLE)
    last = shots[-1]
    assert abs(last["cumulative_xg_home"] - 0.80) < 1e-9
    assert abs(last["cumulative_xg_away"] - 0.30) < 1e-9
    assert abs(last["cumulative_xg_total"] - 1.10) < 1e-9
    # Running scoreline: after the 23' away goal it's 0-1, after 40' home goal 1-1.
    assert (shots[1]["score_home"], shots[1]["score_away"]) == (0, 1)
    assert (shots[2]["score_home"], shots[2]["score_away"]) == (1, 1)
    assert (shots[3]["score_home"], shots[3]["score_away"]) == (2, 1)
    # Cumulative series is monotonic non-decreasing.
    totals = [s["cumulative_xg_total"] for s in shots]
    assert totals == sorted(totals)


def test_no_shotmap_returns_empty_shots():
    detail = {"general": {"matchId": "1", "homeTeam": {"name": "X"}, "awayTeam": {"name": "Y"}},
              "header": {"teams": [{"name": "X", "score": 0}, {"name": "Y", "score": 0}]},
              "content": {}}
    meta, shots = parse_match(detail)
    assert shots == []
    assert meta["match_id"] == "1"


def test_isHomeTeam_flag_supported():
    detail = {
        "general": {"matchId": "2", "homeTeam": {"name": "H"}, "awayTeam": {"name": "A"}},
        "header": {"teams": [{"name": "H", "score": 1}, {"name": "A", "score": 0}]},
        "content": {"shotmap": {"shots": [
            {"min": 5, "isHomeTeam": True, "expectedGoals": 0.4, "eventType": "Goal", "playerName": "p"},
        ]}},
    }
    _, shots = parse_match(detail)
    assert shots[0]["team"] == "home"
    assert shots[0]["score_home"] == 1
