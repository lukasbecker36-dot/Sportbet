"""Live xG-signal monitor for a single SofaScore match.

Polls the match shotmap every ``--poll`` seconds, computes the rolling
15-minute xG rate (both teams combined), and prints an alert the first time
the rate crosses ``--threshold`` while the goals-to-trigger market line is
still live.

Defaults match the most robust strategy from the 2024/25 holdout test:

    threshold 0.20, over_2.5, trigger window 30–85 min.

Two modes:

  --live <event-id>     Real-time polling against SofaScore.
  --replay <event-id>   Reconstruct the signal frame from the final shotmap
                        of a completed match, minute by minute. Useful for
                        validating the pipeline against yesterday's games.

Phase-2 scope per HANDOVER.md: alerts only, no auto-staking.
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import time
from dataclasses import dataclass
from typing import Iterable

from live.sofascore import SofaScore

# Defaults derived from the 2024/25 OOS holdout — see results/holdout_2425.csv.
DEFAULT_THRESHOLD = 0.20
DEFAULT_MIN_MIN = 30
DEFAULT_MAX_MIN = 85
DEFAULT_MARKET_LINE = 2.5  # "over 2.5 goals"
WINDOW = 15  # minutes


@dataclass
class Shot:
    minute: int
    is_home: bool
    xg: float
    is_goal: bool

    @classmethod
    def from_sofa(cls, s: dict) -> "Shot":
        return cls(
            minute=int(s.get("time", 0) or 0) + int(s.get("addedTime", 0) or 0),
            is_home=bool(s.get("isHome")),
            xg=float(s.get("xg") or 0.0),
            is_goal=(s.get("shotType") == "goal" or s.get("incidentType") == "goal"),
        )


def parse_shots(raw_shots: Iterable[dict]) -> list[Shot]:
    return sorted(
        (Shot.from_sofa(s) for s in raw_shots),
        key=lambda s: (s.minute, 0 if s.is_home else 1),
    )


def goals_total(shots: Iterable[Shot], up_to_minute: int) -> int:
    return sum(1 for s in shots if s.is_goal and s.minute <= up_to_minute)


def xg_rate_window(shots: list[Shot], minute: int, window: int = WINDOW) -> float:
    lo = minute - window
    return sum(s.xg for s in shots if lo < s.minute <= minute)


def _fmt_score(s: list[Shot], minute: int) -> str:
    h = sum(1 for x in s if x.is_goal and x.is_home and x.minute <= minute)
    a = sum(1 for x in s if x.is_goal and not x.is_home and x.minute <= minute)
    return f"{h}-{a}"


def _emit(label: str, **fields) -> None:
    """Single-line structured alert. Keeps it grep-friendly."""
    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {label} {parts}", flush=True)


def _candidate_alert(
    shots: list[Shot],
    minute: int,
    threshold: float,
    market_line: float,
    min_minute: int,
    max_minute: int,
) -> dict | None:
    """Return alert payload if the strategy fires at this minute, else None."""
    if minute < min_minute or minute > max_minute:
        return None
    rate = xg_rate_window(shots, minute)
    if rate < threshold:
        return None
    gt = goals_total(shots, minute)
    # The market we'd bet is over-(gt+0.5); we only fire on the configured line.
    if gt + 0.5 != market_line:
        return None
    return {
        "minute": minute,
        "xg_rate_15m": round(rate, 4),
        "score": _fmt_score(shots, minute),
        "goals_total": gt,
        "market": f"over_{market_line}",
    }


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #
def replay(event_id: int, threshold: float, market_line: float,
           min_minute: int, max_minute: int) -> int:
    with SofaScore() as s:
        ev = s.event(event_id)
        raw = s.shotmap(event_id)
    shots = parse_shots(raw)
    home, away = ev["homeTeam"]["name"], ev["awayTeam"]["name"]
    final_h = ev.get("homeScore", {}).get("current")
    final_a = ev.get("awayScore", {}).get("current")
    _emit("MATCH", home=home, away=away, ft=f"{final_h}-{final_a}", shots=len(shots))

    fired = 0
    prev_above = False
    end_min = min(max_minute + 1, max((s.minute for s in shots), default=90) + 1)
    for minute in range(min_minute, end_min):
        rate = xg_rate_window(shots, minute)
        is_above = rate >= threshold
        if is_above and not prev_above:
            _emit("CROSSING", minute=minute, xg_rate_15m=round(rate, 3),
                  score=_fmt_score(shots, minute))
        prev_above = is_above
        alert = _candidate_alert(
            shots, minute, threshold, market_line, min_minute, max_minute,
        )
        if alert:
            _emit("SIGNAL", **alert)
            # Hold-to-FT settlement check
            final_total = (final_h or 0) + (final_a or 0)
            won = "win" if final_total > market_line else "lose"
            _emit("SETTLEMENT", minute=alert["minute"], market=alert["market"],
                  final_total=final_total, result=won)
            fired += 1
            # In live mode we'd continue polling but not re-fire the same market.
            break
    if fired == 0:
        _emit("NO_SIGNAL", reason="strategy never triggered in window")
    return fired


# --------------------------------------------------------------------------- #
# Live
# --------------------------------------------------------------------------- #
def _match_minute(ev: dict) -> int | None:
    """Best-effort current match minute from event status payload."""
    status = ev.get("status", {})
    if status.get("type") != "inprogress":
        return None
    # SofaScore puts elapsed seconds since current period start in time.played.
    time_block = ev.get("time", {}) or {}
    played = time_block.get("played")
    if played is not None:
        return int(played) // 60
    # Fallback: derive from currentPeriodStartTimestamp + period.
    start = time_block.get("currentPeriodStartTimestamp")
    if start is None:
        return None
    elapsed_s = int(time.time() - start)
    base = 0
    desc = (status.get("description") or "").lower()
    if "2nd" in desc or "second" in desc:
        base = 45
    return base + max(0, elapsed_s // 60)


def live(event_id: int, threshold: float, market_line: float,
         min_minute: int, max_minute: int, poll: float) -> int:
    fired_market = False
    prev_above = False
    with SofaScore() as s:
        _emit("LIVE_START", event_id=event_id, threshold=threshold,
              market_line=market_line, window=f"{min_minute}-{max_minute}",
              poll_s=poll)
        while True:
            try:
                ev = s.event(event_id)
                raw = s.shotmap(event_id)
            except Exception as exc:  # noqa: BLE001
                _emit("FETCH_ERROR", error=str(exc))
                time.sleep(poll)
                continue

            status_type = ev.get("status", {}).get("type")
            status_desc = ev.get("status", {}).get("description")
            if status_type in {"finished", "postponed", "canceled"}:
                _emit("MATCH_END", status=status_desc)
                return 1 if fired_market else 0

            minute = _match_minute(ev)
            shots = parse_shots(raw)
            if minute is None:
                _emit("WAITING", status=status_desc)
                time.sleep(poll)
                continue

            rate = xg_rate_window(shots, minute)
            is_above = rate >= threshold
            gt = goals_total(shots, minute)
            _emit("TICK", minute=minute, status=status_desc,
                  shots=len(shots), score=_fmt_score(shots, minute),
                  xg_rate_15m=round(rate, 3), goals=gt)

            if is_above and not prev_above:
                _emit("CROSSING", minute=minute, xg_rate_15m=round(rate, 3))
            prev_above = is_above

            if not fired_market:
                alert = _candidate_alert(
                    shots, minute, threshold, market_line, min_minute, max_minute,
                )
                if alert:
                    _emit("ALERT", **alert,
                          recommend=f"BACK over {market_line} goals")
                    fired_market = True

            time.sleep(poll)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("event_id", type=int)
    ap.add_argument("--mode", choices=["live", "replay"], default="replay")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--market-line", type=float, default=DEFAULT_MARKET_LINE)
    ap.add_argument("--min-minute", type=int, default=DEFAULT_MIN_MIN)
    ap.add_argument("--max-minute", type=int, default=DEFAULT_MAX_MIN)
    ap.add_argument("--poll", type=float, default=30.0,
                    help="Live mode: seconds between polls.")
    args = ap.parse_args()

    if args.mode == "replay":
        return 0 if replay(args.event_id, args.threshold, args.market_line,
                           args.min_minute, args.max_minute) >= 0 else 1
    return 0 if live(args.event_id, args.threshold, args.market_line,
                     args.min_minute, args.max_minute, args.poll) >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
