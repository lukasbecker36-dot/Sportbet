"""Backtest of TWO concurrent strategies on PL with a one-bet-per-match guard.

Strategies under test:
  A: "early-edge"   xG ≥ 0.35  /  minute 15-55  /  line = goals_at_trigger + 1.5
  B: "late"         xG ≥ 0.20  /  minute 55-85  /  line = goals_at_trigger + 0.5

For each match we collect every trigger from A and B, sort by minute, and
keep the EARLIEST one only. That trigger's outcome (win/lose) is the match's
contribution to the combined P&L.

Reports the standalone P&L for each strategy and the dedup'd combined P&L,
per season and pooled.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from db.connection import get_conn  # noqa: E402


_ODDS = {0: "betfair_over15_odds", 1: "betfair_over25_odds",
         2: "betfair_over35_odds", 3: "betfair_over35_odds"}


def _adj(o: float) -> float:
    n = (o - 1.0) * (1.0 - config.BETFAIR_COMMISSION) + 1.0
    return 1.0 + (n - 1.0) * config.ODDS_HAIRCUT


def _build(sigs: pd.DataFrame, *, threshold: float, mn: int, mx: int,
           market_line: float | None = None, line_offset: float | None = None,
           goal_states: list[int] | None = None, name: str = "?") -> pd.DataFrame:
    """Two ways to define the bet line:
       - market_line=X: fixed line X for every trigger (e.g. always back over_2.5)
       - line_offset=Y: line = goals_at_trigger + Y per trigger (e.g. early-edge)
       Exactly one of the two must be provided.

       goal_states: which goals_at_trigger values to admit. Defaults to [0,1,2]
       for line_offset (so the resulting line maps to a real odds column),
       or [floor(market_line)] for market_line."""
    if (market_line is None) == (line_offset is None):
        raise ValueError("provide exactly one of market_line / line_offset")

    sub = sigs[
        (sigs.signal_value >= threshold)
        & (sigs.trigger_minute >= mn)
        & (sigs.trigger_minute <= mx)
    ].copy()

    if market_line is not None:
        states = goal_states or [int(market_line)]  # floor(2.5)=2
        sub = sub[sub.goals_total_at_trigger.isin(states)]
        sub["line"] = float(market_line)
    else:
        states = goal_states or [0, 1, 2]
        sub = sub[sub.goals_total_at_trigger.isin(states)]
        sub["line"] = sub.goals_total_at_trigger + line_offset

    sub["strategy"] = name
    odds_lookup = {0.5: "betfair_over05_odds", 1.5: "betfair_over15_odds",
                   2.5: "betfair_over25_odds", 3.5: "betfair_over35_odds"}
    sub["odds_col"] = sub.line.map(odds_lookup)
    sub["odds"] = sub.apply(lambda r: r.get(r["odds_col"]) if r["odds_col"] else None, axis=1)
    sub = sub[sub.odds.notna()].copy()
    sub["won"] = (sub.final_total > sub.line).astype(int)
    sub["adj"] = sub.odds.apply(_adj)
    sub["profit_u"] = np.where(sub.won == 1, sub.adj - 1.0, -1.0)
    return sub


def _summary(df: pd.DataFrame, label: str) -> dict:
    n = len(df)
    if n == 0:
        return {"strategy": label, "season": "—", "n": 0, "win_rate": float("nan"),
                "avg_odds": float("nan"), "ev_u": float("nan"), "pnl_£10": 0}
    return {
        "strategy": label,
        "n": n,
        "win_rate": round(float(df.won.mean()), 4),
        "avg_odds": round(float(df.odds.mean()), 3),
        "ev_u": round(float(df.profit_u.mean()), 4),
        "pnl_£10": int(round(df.profit_u.sum() * 10)),
    }


def main() -> int:
    conn = get_conn()
    sigs = pd.read_sql_query(
        """
        SELECT s.*, m.season, m.total_goals AS final_total
        FROM signals s JOIN matches m ON m.match_id = s.match_id
        WHERE m.league = 'ENG-Premier League'
          AND s.signal_type = 'xg_rate_15m'
        """,
        conn,
    )
    conn.close()

    early = _build(sigs, threshold=0.35, mn=15, mx=55,
                   line_offset=1.5, goal_states=[0, 1, 2], name="early")
    late = _build(sigs, threshold=0.20, mn=55, mx=85,
                  market_line=2.5, name="late")  # current PL strat: G=2, over_2.5

    combined_pool = pd.concat([early, late], ignore_index=True)
    combined_pool.sort_values(["match_id", "trigger_minute"], inplace=True)
    # One bet per match: take earliest trigger.
    dedup = combined_pool.drop_duplicates(subset="match_id", keep="first").reset_index(drop=True)

    rows = []
    for season_lbl, mask_fn in [
        ("ALL", lambda df: df),
        ("2024/2025", lambda df: df[df.season == "2024/2025"]),
        ("2025/2026", lambda df: df[df.season == "2025/2026"]),
    ]:
        e = mask_fn(early)
        l = mask_fn(late)
        c = mask_fn(dedup)
        for tag, df in [("EARLY only", e), ("LATE only", l),
                        ("COMBINED (dedup'd)", c)]:
            r = _summary(df, tag)
            r["season"] = season_lbl
            rows.append(r)

    out = pd.DataFrame(rows)[["season", "strategy", "n", "win_rate",
                              "avg_odds", "ev_u", "pnl_£10"]]
    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 50)
    print("=== Standalone vs combined-with-dedup, PL (both seasons) ===")
    print(out.to_string(index=False))
    print()

    # Overlap breakdown
    overlap_matches = (
        early[["match_id"]].drop_duplicates()
        .merge(late[["match_id"]].drop_duplicates(), on="match_id")
    )
    print(f"Matches where BOTH fired: {len(overlap_matches)}  "
          f"(of {sigs.match_id.nunique()} PL matches in the DB)")
    # In dedup, what fraction is each origin?
    by_origin = dedup.strategy.value_counts().to_dict()
    print(f"Dedup'd trigger origin: {by_origin}")
    print()

    out_path = ROOT / "results" / "combined_strategy_pl.csv"
    out.to_csv(out_path, index=False)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
