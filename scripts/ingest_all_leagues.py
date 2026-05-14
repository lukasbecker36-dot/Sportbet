"""One-shot ingest of all four extra leagues x two seasons from Understat.

Runs sequentially (soccerdata's TLS client isn't safe to share across threads).
Re-runs are idempotent — soccerdata caches the raw HTML in
``~/soccerdata/data/Understat``, and our SQLite uses INSERT OR REPLACE.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

logging.disable(logging.CRITICAL)

from scrapers.understat import ingest_understat  # noqa: E402

LEAGUES = [
    "ESP-La Liga",
    "ITA-Serie A",
    "GER-Bundesliga",
    "FRA-Ligue 1",
]
SEASONS = ("2024/2025", "2025/2026")


def main() -> int:
    total_inserted = 0
    total_shots = 0
    summary = []
    for league in LEAGUES:
        for season in SEASONS:
            res = ingest_understat(league=league, seasons=(season,))
            summary.append((league, season, res["inserted"], res["shots"]))
            total_inserted += res["inserted"]
            total_shots += res["shots"]
            print(
                f"  {league:<22} {season}: "
                f"{res['inserted']:>3} matches, {res['shots']:>5} shots"
            )
    print()
    print(f"TOTAL: {total_inserted} matches, {total_shots} shots")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
