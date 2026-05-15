"""Quick analysis: 'early edge' xG-rate strategy on PL 24/25 + 25/26.

Strategy variant the user proposed:
  - Trigger window: minutes 15–55
  - On the first xG-rate-15m crossing above THRESHOLD inside that window
  - Back the over-line such that 2+ more goals are still needed
    (i.e. market = over_(goals_total + 1.5))
  - Win = match final total > that line

For each threshold we report sample size, win rate, and a rough EV using a
fixed 2.0 decimal-odds heuristic (Betfair odds aren't attached at minute 15–29
triggers in our existing pipeline). Real EV depends on what the live LTP
actually shows.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_conn  # noqa: E402


LEAGUE = "ENG-Premier League"
WINDOW_MIN = 15
WINDOW_MAX = 55
ROLL = 15
THRESHOLDS = [0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.60]


def detect_first_crossing(match_id: str, conn) -> dict | None:
    """Return the first xg_rate_15m crossing in [WINDOW_MIN, WINDOW_MAX]."""
    shots = pd.read_sql_query(
        "SELECT minute, team, xg, is_goal FROM shots "
        "WHERE match_id=? ORDER BY minute, id",
        conn, params=(match_id,),
    )
    if shots.empty:
        return None
    last_minute = max(int(shots["minute"].max()), 90)
    idx = pd.RangeIndex(0, last_minute + 1, name="minute")

    xg_min = shots.groupby("minute")["xg"].sum().reindex(idx, fill_value=0.0)
    goals_min = shots.groupby("minute")["is_goal"].sum().reindex(idx, fill_value=0).astype(int)
    xg_rate = xg_min.rolling(ROLL, min_periods=1).sum()
    goals_cum = goals_min.cumsum()
    return {"xg_rate": xg_rate, "goals_cum": goals_cum}


def main() -> int:
    conn = get_conn()
    matches = pd.read_sql_query(
        "SELECT match_id, season, total_goals AS final_total "
        "FROM matches WHERE league=? AND total_goals IS NOT NULL",
        conn, params=(LEAGUE,),
    )
    print(f"PL matches: {len(matches)} ({matches.season.value_counts().to_dict()})")
    print()

    # For each match, compute the rolling-rate series once.
    series_by_match: dict[str, dict] = {}
    for mid in matches["match_id"]:
        s = detect_first_crossing(mid, conn)
        if s:
            series_by_match[mid] = s
    print(f"matches with shot data: {len(series_by_match)}")

    rows = []
    for threshold in THRESHOLDS:
        triggers = []
        for _, m in matches.iterrows():
            s = series_by_match.get(m["match_id"])
            if s is None:
                continue
            xg_rate = s["xg_rate"]
            goals_cum = s["goals_cum"]
            # First crossing of threshold inside [15, 55]
            above = xg_rate >= threshold
            window = above.loc[WINDOW_MIN:WINDOW_MAX]
            prev = bool(above.loc[WINDOW_MIN - 1]) if WINDOW_MIN >= 1 else False
            fire_min = None
            for minute, is_above in window.items():
                if is_above and not prev:
                    fire_min = int(minute); break
                prev = bool(is_above)
            if fire_min is None:
                continue
            G = int(goals_cum.loc[fire_min])
            line = G + 1.5  # the 2-more-goals line
            won = m["final_total"] > line
            triggers.append({
                "season": m["season"], "fire_min": fire_min,
                "goals_at_trigger": G, "line": line,
                "final_total": int(m["final_total"]), "won": int(won),
                "rate_at_trigger": float(xg_rate.loc[fire_min]),
            })
        df = pd.DataFrame(triggers)
        if df.empty:
            continue
        for season, sub in [("ALL", df), ("2024/2025", df[df.season=="2024/2025"]),
                            ("2025/2026", df[df.season=="2025/2026"])]:
            if sub.empty: continue
            row = {
                "threshold": threshold,
                "season": season,
                "n": len(sub),
                "win_rate": round(sub.won.mean(), 4),
                "avg_fire_min": round(sub.fire_min.mean(), 1),
                "avg_goals_at_trigger": round(sub.goals_at_trigger.mean(), 2),
                "avg_line": round(sub.line.mean(), 2),
                # Heuristic EV at flat-2.0 odds, 5% commission, 8% in-play haircut
                "ev@flat_2.00": round(
                    sub.won.mean() * ((2.0 - 1) * 0.95 * 0.92) - (1 - sub.won.mean()), 4,
                ),
            }
            # Implied break-even win-rate at flat 2.0 odds after haircut:
            # net_win = 0.95*0.92 = 0.874   => break-even p = 1 / (1 + 0.874) ≈ 53.4%
            rows.append(row)

    out = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 200)
    print()
    print("=== 'Early edge' (15–55 min, line = goals_at_trigger + 1.5, need 2+ more goals) ===")
    print(out.to_string(index=False))

    out_path = ROOT / "results" / "early_edge_pl.csv"
    out.to_csv(out_path, index=False)
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
