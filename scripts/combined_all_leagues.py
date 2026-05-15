"""Combined (early-edge + late) P&L per league with one-bet-per-match dedup.

Per-league configuration (matching the live monitor's LIVE_STRATEGIES_BY_LEAGUE):

  PL   late = 0.20 over_2.5 55-85 (G=2)       early = 0.40 relative+1.5 15-55
  ESP  late = 0.30 over_1.5 55-80 (G=1)       early = 0.40 relative+1.5 15-55
  ITA  late = 0.25 over_3.5 30-85 (G=3)       early = 0.40 relative+1.5 15-55
  GER  late = 0.20 over_2.5 30-75 (G=2)       early = disabled (unstable)
  FRA  late = 0.50 over_1.5 30-80 (G=1)       early = disabled (unstable)

For matches where both strategies fire, the EARLIEST trigger wins (dedup).
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
    "ENG-Premier League": "PL", "ESP-La Liga": "ESP",
    "ITA-Serie A": "ITA",       "GER-Bundesliga": "GER",
    "FRA-Ligue 1": "FRA",
}
ODDS_FOR_LINE = {0.5: "betfair_over05_odds", 1.5: "betfair_over15_odds",
                 2.5: "betfair_over25_odds", 3.5: "betfair_over35_odds"}

# (threshold, line_kind, line_value, min_minute, max_minute)
# line_kind:
#   "fixed"    -> back over_{line_value} when goals_at_trigger == floor(line_value)
#   "relative" -> back over_{goals+line_value}    (e.g. line_value=1.5 = "need 2 more")
LATE = {
    "PL":  (0.20, "fixed",    2.5, 55, 85),
    "ESP": (0.30, "fixed",    1.5, 55, 80),
    "ITA": (0.25, "fixed",    3.5, 30, 85),
    "GER": (0.20, "fixed",    2.5, 30, 75),
    "FRA": (0.50, "fixed",    1.5, 30, 80),
}
EARLY = {  # disabled leagues simply not in the dict
    "PL":  (0.40, "relative", 1.5, 15, 55),
    "ESP": (0.40, "relative", 1.5, 15, 55),
    "ITA": (0.40, "relative", 1.5, 15, 55),
}


def _adj(o: float) -> float:
    n = (o - 1.0) * (1.0 - config.BETFAIR_COMMISSION) + 1.0
    return 1.0 + (n - 1.0) * config.ODDS_HAIRCUT


def _select(sigs: pd.DataFrame, threshold: float, kind: str, val: float,
            mn: int, mx: int, *, name: str) -> pd.DataFrame:
    sub = sigs[
        (sigs.signal_value >= threshold)
        & (sigs.trigger_minute >= mn) & (sigs.trigger_minute <= mx)
    ].copy()
    if kind == "fixed":
        sub = sub[sub.goals_total_at_trigger == math.floor(val)]
        sub["line"] = val
    else:
        sub = sub[sub.goals_total_at_trigger.isin([0, 1, 2])]
        sub["line"] = sub.goals_total_at_trigger + val
    sub["strategy"] = name
    sub["odds_col"] = sub.line.map(ODDS_FOR_LINE)
    sub["odds"] = sub.apply(lambda r: r.get(r["odds_col"]) if r["odds_col"] else None, axis=1)
    sub = sub[sub.odds.notna()].copy()
    sub["won"] = (sub.final_total > sub.line).astype(int)
    sub["adj"] = sub.odds.apply(_adj)
    sub["profit_u"] = np.where(sub.won == 1, sub.adj - 1.0, -1.0)
    return sub


def _summarise(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"n": 0, "win_rate": float("nan"), "avg_odds": float("nan"),
                "ev_u": float("nan"), "pnl_£10": 0}
    return {
        "n": int(len(df)),
        "win_rate": round(float(df.won.mean()), 4),
        "avg_odds": round(float(df.odds.mean()), 3),
        "ev_u": round(float(df.profit_u.mean()), 4),
        "pnl_£10": int(round(df.profit_u.sum() * 10)),
    }


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
    grand_pnl = {"late": 0, "early": 0, "combined": 0}
    grand_n   = {"late": 0, "early": 0, "combined": 0}

    for league_full, league_short in LEAGUE_SHORT.items():
        L = sigs[sigs.league == league_full]
        # LATE
        l = _select(L, *LATE[league_short], name="late")
        # EARLY (if league has one)
        e = (_select(L, *EARLY[league_short], name="early")
             if league_short in EARLY else l.iloc[0:0].copy())
        # Combined with dedup: take earliest per match.
        pool = pd.concat([l, e], ignore_index=True)
        pool.sort_values(["match_id", "trigger_minute"], inplace=True)
        combined = pool.drop_duplicates(subset="match_id", keep="first").reset_index(drop=True)

        for season_lbl, mask in [("ALL", slice(None)),
                                  ("2024/2025", l.season == "2024/2025"),
                                  ("2025/2026", l.season == "2025/2026")]:
            if season_lbl == "ALL":
                l_s, e_s, c_s = l, e, combined
            else:
                l_s = l[l.season == season_lbl]
                e_s = e[e.season == season_lbl] if not e.empty else e
                c_s = combined[combined.season == season_lbl]
            for tag, df in [("LATE", l_s), ("EARLY", e_s), ("COMBINED", c_s)]:
                row = {"league": league_short, "season": season_lbl, "strategy": tag}
                row.update(_summarise(df))
                rows.append(row)

        # Grand totals across leagues (using pooled-2-seasons figures)
        grand_pnl["late"]     += int(round(l.profit_u.sum() * 10))
        grand_pnl["early"]    += int(round(e.profit_u.sum() * 10)) if not e.empty else 0
        grand_pnl["combined"] += int(round(combined.profit_u.sum() * 10))
        grand_n["late"]     += len(l)
        grand_n["early"]    += len(e) if not e.empty else 0
        grand_n["combined"] += len(combined)

    df = pd.DataFrame(rows)[["league", "season", "strategy", "n", "win_rate",
                             "avg_odds", "ev_u", "pnl_£10"]]
    pd.set_option("display.width", 220)
    pd.set_option("display.max_rows", 200)
    print(df.to_string(index=False))
    print()
    print("=== Grand totals across all 5 leagues, 2 seasons combined ===")
    for k in ["late", "early", "combined"]:
        print(f"  {k:>9}: n={grand_n[k]:>4}  P&L @ £10 flat = £{grand_pnl[k]:>5}")
    print(f"  per season (combined, /2): £{grand_pnl['combined']//2}")

    out_path = ROOT / "results" / "combined_all_leagues.csv"
    df.to_csv(out_path, index=False)
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
