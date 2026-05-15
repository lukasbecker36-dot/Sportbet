"""Append-only CSV log of settled paper trades.

Each settled alert appends one row. /pnl computes cumulative stats by
streaming the file — durable across bot restarts.
"""

from __future__ import annotations

import csv
import os
import threading
from pathlib import Path
from typing import Iterable

import config

_FIELDS = (
    "ts_alert", "ts_settled",
    "event_id", "league", "home", "away",
    "minute", "score", "xg_rate",
    "market", "market_line", "price",
    "ev", "mode",
    "ft_total", "won",
    "paper_stake", "paper_profit",
)

_lock = threading.Lock()


def _path() -> Path:
    return Path(getattr(config, "PAPER_TRADES_CSV", "data/paper_trades.csv"))


def _ensure_header(path: Path) -> None:
    if path.exists() and path.stat().st_size > 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow(_FIELDS)


def append_settled(alert: dict, *, ts_settled: str) -> None:
    """Append one settled-alert row. ``alert`` is the in-memory dict from
    STATE.recent_alerts after _settle_alerts_for_match has filled the
    settlement fields."""
    path = _path()
    with _lock:
        _ensure_header(path)
        row = [
            alert.get("ts"), ts_settled,
            alert.get("event_id"), alert.get("league", ""),
            alert.get("home"), alert.get("away"),
            alert.get("minute"), alert.get("score"), alert.get("xg_rate"),
            alert.get("market"), alert.get("market_line"), alert.get("price"),
            alert.get("ev"), alert.get("mode"),
            alert.get("final_total"), int(bool(alert.get("won"))),
            float(getattr(config, "PAPER_STAKE_GBP", 5.0)),
            alert.get("paper_profit_gbp"),
        ]
        with path.open("a", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow(row)


def stats() -> dict:
    """Return cumulative paper-trade stats from the on-disk CSV."""
    path = _path()
    if not path.exists():
        return {"n": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "total_pnl": 0.0, "total_staked": 0.0, "roi_pct": 0.0,
                "leagues": {}, "first_ts": None, "last_ts": None}
    wins = losses = 0
    total_pnl = 0.0
    total_staked = 0.0
    first_ts: str | None = None
    last_ts: str | None = None
    by_league: dict[str, dict] = {}
    with _lock, path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                won = bool(int(row.get("won") or "0"))
                profit = float(row.get("paper_profit") or 0.0)
                stake = float(row.get("paper_stake") or 0.0)
            except (TypeError, ValueError):
                continue
            if won:
                wins += 1
            else:
                losses += 1
            total_pnl += profit
            total_staked += stake
            ts = row.get("ts_settled") or row.get("ts_alert") or ""
            if ts:
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts
            league = row.get("league") or "?"
            lg = by_league.setdefault(
                league, {"n": 0, "wins": 0, "pnl": 0.0, "staked": 0.0},
            )
            lg["n"] += 1
            lg["wins"] += 1 if won else 0
            lg["pnl"] += profit
            lg["staked"] += stake
    n = wins + losses
    return {
        "n": n, "wins": wins, "losses": losses,
        "win_rate": (wins / n) if n else 0.0,
        "total_pnl": round(total_pnl, 2),
        "total_staked": round(total_staked, 2),
        "roi_pct": (total_pnl / total_staked * 100.0) if total_staked else 0.0,
        "leagues": by_league,
        "first_ts": first_ts, "last_ts": last_ts,
    }
