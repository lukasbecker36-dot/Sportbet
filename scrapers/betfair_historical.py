"""Parser for Betfair historical price files (bz2-compressed CSV).

Source: https://historicdata.betfair.com/  (free Betfair account required).
Drop downloaded files into config.BETFAIR_DATA_DIR.

Expected columns include: MARKET_TIME, SELECTION_NAME, LAST_PRICE_TRADED,
MATCHED_AMOUNT, EVENT_NAME (e.g. "Home Team v Away Team"), MARKET_NAME.
"""

import bz2
import csv
import os
import re
from typing import Iterator

import config
from utils.logging import get_logger
from utils.teams import normalise

logger = get_logger("betfair")

_OVER_SELECTION_RE = re.compile(r"^Over\s+(\d+\.\d)\s+Goals$", re.IGNORECASE)
_EVENT_VS_RE = re.compile(r"\s+v\s+|\s+vs\.?\s+", re.IGNORECASE)

# Map a goal line to the signals-table column it populates.
LINE_TO_ODDS_COL = {
    0.5: "betfair_over05_odds",
    1.5: "betfair_over15_odds",
    2.5: "betfair_over25_odds",
    3.5: "betfair_over35_odds",
}


def net_odds(odds: float) -> float:
    """Decimal odds after Betfair commission."""
    return (odds - 1.0) * (1.0 - config.BETFAIR_COMMISSION) + 1.0


def list_files(directory: str | None = None) -> list[str]:
    directory = directory or config.BETFAIR_DATA_DIR
    if not os.path.isdir(directory):
        return []
    return [
        os.path.join(directory, f)
        for f in sorted(os.listdir(directory))
        if f.endswith(".bz2") or f.endswith(".csv")
    ]


def _open(filepath: str):
    if filepath.endswith(".bz2"):
        return bz2.open(filepath, "rt", newline="")
    return open(filepath, "rt", newline="")


def iter_price_rows(filepath: str) -> Iterator[dict]:
    with _open(filepath) as fh:
        yield from csv.DictReader(fh)


def _parse_event_teams(event_name: str | None) -> tuple[str, str] | None:
    if not event_name:
        return None
    parts = _EVENT_VS_RE.split(event_name, maxsplit=1)
    if len(parts) != 2:
        return None
    return normalise(parts[0]), normalise(parts[1])


def extract_over_goals_prices(filepath: str) -> list[dict]:
    """Return Over-N.5-Goals price snapshots from one file.

    Each record: {line, market_time, last_price_traded, matched_amount,
                  home, away, event_name}
    """
    out: list[dict] = []
    for row in iter_price_rows(filepath):
        selection = (row.get("SELECTION_NAME") or "").strip()
        m = _OVER_SELECTION_RE.match(selection)
        if not m:
            continue
        line = float(m.group(1))
        teams = _parse_event_teams(row.get("EVENT_NAME"))
        try:
            last_price = float(row["LAST_PRICE_TRADED"]) if row.get("LAST_PRICE_TRADED") else None
        except ValueError:
            last_price = None
        try:
            matched = float(row["MATCHED_AMOUNT"]) if row.get("MATCHED_AMOUNT") else None
        except ValueError:
            matched = None
        out.append({
            "line": line,
            "market_time": row.get("MARKET_TIME"),
            "last_price_traded": last_price,
            "matched_amount": matched,
            "home": teams[0] if teams else None,
            "away": teams[1] if teams else None,
            "event_name": row.get("EVENT_NAME"),
        })
    logger.info("Parsed %d over-goals price rows from %s", len(out), os.path.basename(filepath))
    return out
