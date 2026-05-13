"""Parser for Betfair historical price files.

Source: https://historicdata.betfair.com/  (free Betfair account required).
Drop downloaded files into config.BETFAIR_DATA_DIR (nested subdirectories OK).

Two on-disk formats are supported transparently:

  * Streaming JSON (the "BASIC"/"PRO" exports): one bz2 file per market, newline-
    delimited JSON ("mcm" messages) — first message carries the marketDefinition,
    later ones carry runner-change price updates with millisecond timestamps.
  * Legacy CSV exports: one row per price snapshot with EVENT_NAME / MARKET_NAME /
    SELECTION_NAME / MARKET_TIME / LAST_PRICE_TRADED / MATCHED_AMOUNT columns.
"""

import bz2
import csv
import json
import os
import re
from datetime import datetime, timezone
from typing import Iterator

import config
from utils.logging import get_logger
from utils.teams import normalise

logger = get_logger("betfair")

_OVER_SELECTION_RE = re.compile(r"^Over\s+(\d+\.\d)\s+Goals$", re.IGNORECASE)
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


def _open_text(filepath: str):
    if filepath.endswith(".bz2"):
        return bz2.open(filepath, "rt", newline="")
    return open(filepath, "rt", newline="")


def _parse_event_teams(event_name: str | None) -> tuple[str, str] | None:
    if not event_name:
        return None
    parts = _EVENT_VS_RE.split(event_name, maxsplit=1)
    if len(parts) != 2:
        return None
    return normalise(parts[0]), normalise(parts[1])


def _iso_from_pt(pt) -> str | None:
    if pt is None:
        return None
    return datetime.fromtimestamp(float(pt) / 1000.0, tz=timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Streaming-JSON format
# --------------------------------------------------------------------------- #
def parse_market(filepath: str) -> dict | None:
    """Parse one streaming-JSON market file into structured form.

    Returns None if the file isn't a recognised over-goals market.
    Otherwise:
        {home, away, line, kickoff, event_name,
         snapshots: [(timestamp_iso, last_traded_price), ...]}   # time-ordered
    where ``kickoff`` is the scheduled market start (ISO str) or None.
    """
    try:
        with _open_text(filepath) as fh:
            raw_lines = [ln.strip() for ln in fh if ln.strip()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cannot open %s: %s", filepath, exc)
        return None
    if not raw_lines:
        return None

    try:
        first_msg = json.loads(raw_lines[0])
    except json.JSONDecodeError:
        return None  # not streaming JSON

    mc_list = first_msg.get("mc", [])
    if not mc_list:
        return None
    md = mc_list[0].get("marketDefinition", {})

    line = _MARKET_TYPE_TO_LINE.get(md.get("marketType", ""))
    if line is None:
        return None
    teams = _parse_event_teams(md.get("eventName", ""))
    if not teams:
        return None
    home, away = teams

    over_runner_id = None
    for runner in md.get("runners", []):
        if "over" in runner.get("name", "").lower():
            over_runner_id = runner.get("id")
            break
    if over_runner_id is None:
        return None

    kickoff = md.get("marketTime") or md.get("openDate")

    snapshots: list[tuple[str, float]] = []
    # First message may itself carry rc entries; scan all messages uniformly.
    for raw_line in raw_lines:
        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        ts = _iso_from_pt(msg.get("pt"))
        for market in msg.get("mc", []):
            for rc in market.get("rc", []):
                if rc.get("id") == over_runner_id and "ltp" in rc and ts:
                    snapshots.append((ts, float(rc["ltp"])))
    snapshots.sort(key=lambda t: t[0])

    return {
        "home": home, "away": away, "line": line,
        "kickoff": kickoff, "event_name": md.get("eventName", ""),
        "snapshots": snapshots,
    }


# --------------------------------------------------------------------------- #
# Legacy CSV format
# --------------------------------------------------------------------------- #
def iter_price_rows(filepath: str) -> Iterator[dict]:
    with _open_text(filepath) as fh:
        yield from csv.DictReader(fh)


def _extract_csv(filepath: str) -> list[dict]:
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
    return out


def _looks_like_json(filepath: str) -> bool:
    try:
        with _open_text(filepath) as fh:
            for ln in fh:
                ln = ln.strip()
                if ln:
                    return ln.startswith("{")
    except Exception:  # noqa: BLE001
        return False
    return False


def extract_over_goals_prices(filepath: str) -> list[dict]:
    """Flat list of Over-N.5-Goals price snapshots from one file (any format).

    Each record: {line, market_time, last_price_traded, matched_amount,
                  home, away, event_name}
    """
    if _looks_like_json(filepath):
        market = parse_market(filepath)
        if not market:
            return []
        out = [{
            "line": market["line"],
            "market_time": ts,
            "last_price_traded": price,
            "matched_amount": None,
            "home": market["home"],
            "away": market["away"],
            "event_name": market["event_name"],
        } for ts, price in market["snapshots"]]
    else:
        out = _extract_csv(filepath)
    logger.info("Parsed %d over-goals price rows from %s", len(out), os.path.basename(filepath))
    return out
