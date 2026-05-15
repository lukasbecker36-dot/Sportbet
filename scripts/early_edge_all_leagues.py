"""Apply the early-edge variant (15-55 min, line=goals+1.5) to all 5 leagues.

Pulls signals from the table (which already has the lowered floor of 15 after
early_edge_with_odds.py was run) and reports per-league per-season EV across
a small grid of thresholds. Picks the most stable threshold per league.
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

LEAGUE_SHORT = {
    "ENG-Premier League": "PL",
    "ESP-La Liga": "ESP",
    "ITA-Serie A": "ITA",
    "GER-Bundesliga": "GER",
    "FRA-Ligue 1": "FRA",
}
ODDS_FOR_LINE = {1.5: "betfair_over15_odds", 2.5: "betfair_over25_odds",
                 3.5: "betfair_over35_odds"}
THRESHOLDS = [0.25, 0.30, 0.35, 0.40]


def _adj(o: float) -> float:
    n = (o - 1.0) * (1.0 - config.BETFAIR_COMMISSION) + 1.0
    return 1.0 + (n - 1.0) * config.ODDS_HAIRCUT


def main() -> int:
    conn = get_conn()
    sigs = pd.read_sql_query(
        """
        SELECT s.*, m.league, m.season, m.total_goals AS final_total
        FROM signals s JOIN matches m ON m.match_id = s.match_id
        WHERE s.signal_type='xg_rate_15m'
        """,
        conn,
    )
    conn.close()

    rows = []
    for league in sigs.league.unique():
        league_sub = sigs[sigs.league == league]
        for thr in THRESHOLDS:
            sub = league_sub[
                (league_sub.signal_value >= thr)
                & (league_sub.trigger_minute >= 15)
                & (league_sub.trigger_minute <= 55)
                & (league_sub.goals_total_at_trigger.isin([0, 1, 2]))
            ].copy()
            sub["line"] = sub.goals_total_at_trigger + 1.5
            sub["odds"] = sub.apply(lambda r: r[ODDS_FOR_LINE[r.line]], axis=1)
            priced = sub[sub.odds.notna()].copy()
            if priced.empty:
                continue
            priced["won"] = (priced.final_total > priced.line).astype(int)
            priced["adj"] = priced.odds.apply(_adj)
            priced["profit_u"] = np.where(priced.won == 1, priced.adj - 1.0, -1.0)
            for season_lbl in ["2024/2025", "2025/2026"]:
                s = priced[priced.season == season_lbl]
                if len(s) < 15:
                    continue
                rows.append({
                    "league": LEAGUE_SHORT.get(league, league),
                    "thr": thr,
                    "season": season_lbl,
                    "n": int(len(s)),
                    "win_rate": round(float(s.won.mean()), 4),
                    "avg_odds": round(float(s.odds.mean()), 3),
                    "ev_u": round(float(s.profit_u.mean()), 4),
                    "pnl_£10": int(round(s.profit_u.sum() * 10)),
                })

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 220)
    pd.set_option("display.max_rows", 200)
    print("=== Early-edge (xG≥thr, 15-55 min, line=goals+1.5) — per league per season ===")
    print(df.to_string(index=False))
    print()

    # Pick the most robust threshold per league = highest min(EV across seasons),
    # subject to n_per_season >= 20.
    print("=== Robust early-edge pick per league ===")
    picks = []
    for league in df.league.unique():
        L = df[df.league == league]
        wide = L.pivot_table(index="thr", columns="season",
                             values=["ev_u", "n", "pnl_£10"])
        ev24 = wide.get(("ev_u", "2024/2025"), pd.Series(dtype=float))
        ev25 = wide.get(("ev_u", "2025/2026"), pd.Series(dtype=float))
        n24  = wide.get(("n", "2024/2025"),    pd.Series(dtype=int))
        n25  = wide.get(("n", "2025/2026"),    pd.Series(dtype=int))
        if ev24.empty or ev25.empty:
            continue
        mn_ev = pd.DataFrame({"ev24": ev24, "ev25": ev25, "n24": n24, "n25": n25}).dropna()
        mn_ev["min_ev"] = mn_ev[["ev24", "ev25"]].min(axis=1)
        ok = mn_ev[(mn_ev.n24 >= 20) & (mn_ev.n25 >= 20)]
        if ok.empty:
            print(f"  {league}: no threshold cleared n≥20/season filter")
            continue
        best = ok.sort_values("min_ev", ascending=False).iloc[0]
        picks.append({"league": league, "thr": best.name,
                      "ev_24/25": float(best.ev24), "ev_25/26": float(best.ev25),
                      "n_24/25": int(best.n24), "n_25/26": int(best.n25),
                      "pnl_2yr_£10": int(round((float(best.ev24)*int(best.n24)
                                              + float(best.ev25)*int(best.n25))*10))})
    print(pd.DataFrame(picks).to_string(index=False))

    out_path = ROOT / "results" / "early_edge_all_leagues.csv"
    df.to_csv(out_path, index=False)
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
