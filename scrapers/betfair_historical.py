"""Parser for Betfair historical price files (streaming JSON, bz2-compressed).

Source: https://historicdata.betfair.com/  (free Betfair account required).
Drop downloaded files into config.BETFAIR_DATA_DIR (nested subdirectories OK).

Each .bz2 file is one market in Betfair's streaming format:
  line 1  -> {"op":"mcm","mc":[{"id":"...","marketDefinition":{...}}]}
  line N  -> {"op":"mcm","pt":<ms>,"mc":[{"id":"...","rc":[{"id":<rid>,"ltp":<price>}]}]}
"""

import bz2
import json
import os
import re
from datetime import datetime, timezone
from typing import Iterator

import config
from utils.logging import get_logger
from utils.teams import normalise

logger = get_logger("betfair")

_EVENT_VS_RE = re.compile(r"\s+v\s+|\s+vs\.?\s+", re.IGNORECASE)

_MARKET_TYPE_TO_LINE = {
    "OVER_UNDER_05": 0.5,
    "OVER_UNDER_15": 1.5,
    "OVER_UNDER_25": 2.5,
    "OVER_UNDER_35": 3.5,
}

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
    """Walk directory tree and return all .bz2 / .csv price files."""
    directory = directory or config.BETFAIR_DATA_DIR
    if not os.path.isdir(directory):
        return []
    found = []
    for root, _, fnames in os.walk(directory):
        for f in sorted(fnames):
            if f.endswith(".bz2") or f.endswith(".csv"):
                found.append(os.path.join(root, f))
    return sorted(found)


def _parse_event_teams(event_name: str | None) -> tuple[str, str] | None:
    if not event_name:
        return None
    parts = _EVENT_VS_RE.split(event_name, maxsplit=1)
    if len(parts) != 2:
        return None
    return normalise(parts[0]), normalise(parts[1])


def extract_over_goals_prices(filepath: str) -> list[dict]:
    """Return Over-N.5-Goals price snapshots from one streaming-JSON bz2 file.

    Each record: {line, market_time, last_price_traded, matched_amount,
                  home, away, event_name}
    """
    out: list[dict] = []
    try:
        with bz2.open(filepath, "rt") as fh:
            raw_lines = [ln.strip() for ln in fh if ln.strip()]
    except Exception as exc:
        logger.warning("Cannot open %s: %s", filepath, exc)
        return []

    if not raw_lines:
        return []

    # --- Parse market definition from first message ---
    try:
        first_msg = json.loads(raw_lines[0])
    except json.JSONDecodeError:
        logger.warning("Bad JSON in first line of %s", filepath)
        return []

    mc_list = first_msg.get("mc", [])
    if not mc_list:
        return []
    md = mc_list[0].get("marketDefinition", {})

    market_type = md.get("marketType", "")
    line = _MARKET_TYPE_TO_LINE.get(market_type)
    if line is None:
        return []  # not an over-goals market we care about

    event_name = md.get("eventName", "")
    teams = _parse_event_teams(event_name)
    if not teams:
        return []
    home, away = teams

    # Find the runner ID for "Over X Goals" (not "Under")
    over_runner_id: int | None = None
    for runner in md.get("runners", []):
        if "over" in runner.get("name", "").lower():
            over_runner_id = runner.get("id")
            break
    if over_runner_id is None:
        return []

    # --- Parse subsequent price-update messages ---
    for raw_line in raw_lines[1:]:
        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        pt = msg.get("pt")  # unix timestamp in milliseconds
        mc_updates = msg.get("mc", [])
        for market in mc_updates:
            for rc in market.get("rc", []):
                if rc.get("id") == over_runner_id and "ltp" in rc:
                    market_time = (
                        datetime.fromtimestamp(pt / 1000.0, tz=timezone.utc).isoformat()
                        if pt is not None else None
                    )
                    out.append({
                        "line": line,
                        "market_time": market_time,
                        "last_price_traded": float(rc["ltp"]),
                        "matched_amount": None,
                        "home": home,
                        "away": away,
                        "event_name": event_name,
                    })

    logger.info("Parsed %d over-goals price rows from %s", len(out), os.path.basename(filepath))
    return out
