"""Per-league in-sample vs 2024/25 holdout, run across the full grid.

For every (threshold, market, min_minute, max_minute) cell in the standard
backtest grid, compute win-rate + EV separately for each (league × season)
slice. Writes one CSV per league plus a combined view that picks the most
robust strategy per league.

A "robust" candidate must have:
  - n_priced ≥ 30 in BOTH seasons (else the EV figure is too noisy)
  - min(EV_24_25, EV_25_26) ≥ MIN_ROBUST_EV

Output: results/per_league/<league_short>.csv  +  results/per_league_best.csv.
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


MIN_ROBUST_EV = 0.05
MIN_ROBUST_N_PRICED_PER_SEASON = 30

LEAGUE_SHORT = {
    "ENG-Premier League": "PL",
    "ESP-La Liga": "ESP",
    "ITA-Serie A": "ITA",
    "GER-Bundesliga": "GER",
    "FRA-Ligue 1": "FRA",
}

_MARKET_ODDS_COL = {
    "over_0.5": "betfair_over05_odds",
    "over_1.5": "betfair_over15_odds",
    "over_2.5": "betfair_over25_odds",
    "over_3.5": "betfair_over35_odds",
}


def load_signals() -> pd.DataFrame:
    conn = get_conn()
    try:
        return pd.read_sql_query(
            """
            SELECT s.*, m.league, m.season, m.total_goals AS final_total_goals
            FROM signals s JOIN matches m ON m.match_id = s.match_id
            WHERE s.signal_type = 'xg_rate_15m'
            """,
            conn,
        )
    finally:
        conn.close()


def _adj(odds: pd.Series) -> pd.Series:
    comm = 1.0 - config.BETFAIR_COMMISSION
    net = (odds - 1.0) * comm + 1.0
    return 1.0 + (net - 1.0) * config.ODDS_HAIRCUT


def evaluate(subset: pd.DataFrame, market: str) -> dict:
    line = float(market.split("_", 1)[1])
    floor_line = math.floor(line)
    sub = subset[subset["goals_total_at_trigger"] == floor_line]
    out = {"n": int(len(sub)), "win_rate": np.nan,
           "n_priced": 0, "win_rate_priced": np.nan,
           "avg_odds": np.nan, "ev": np.nan}
    if not len(sub):
        return out
    wins = (sub["final_total_goals"] > line).astype(int)
    out["win_rate"] = round(float(wins.mean()), 4)

    odds_col = _MARKET_ODDS_COL[market]
    priced = sub[sub[odds_col].notna()]
    if len(priced):
        pw = (priced["final_total_goals"] > line).astype(int)
        adj = _adj(priced[odds_col])
        profit = np.where(pw == 1, adj - 1.0, -1.0)
        out.update({
            "n_priced": int(len(priced)),
            "win_rate_priced": round(float(pw.mean()), 4),
            "avg_odds": round(float(priced[odds_col].mean()), 3),
            "ev": round(float(profit.mean()), 4),
        })
    return out


def grid() -> list[tuple]:
    out = []
    for threshold in config.XG_RATE_THRESHOLDS:
        for market in config.MARKETS:
            for mn in config.MIN_MINUTE:
                for mx in config.MAX_MINUTE:
                    if mn >= mx:
                        continue
                    out.append((threshold, market, mn, mx))
    return out


def main() -> int:
    sigs = load_signals()
    seasons = ["2024/2025", "2025/2026"]
    leagues = sorted(sigs["league"].unique())
    print(f"signals total: {len(sigs)}; leagues: {leagues}; seasons: {seasons}")

    out_dir = ROOT / "results" / "per_league"
    out_dir.mkdir(parents=True, exist_ok=True)

    summary_rows = []
    for league in leagues:
        league_short = LEAGUE_SHORT.get(league, league[:3].upper())
        rows: list[dict] = []
        for thr, market, mn, mx in grid():
            league_sigs = sigs[(sigs["league"] == league) &
                               (sigs["signal_value"] >= thr) &
                               (sigs["trigger_minute"] >= mn) &
                               (sigs["trigger_minute"] <= mx)]
            res = {}
            for season in seasons:
                res[season] = evaluate(league_sigs[league_sigs["season"] == season], market)
            both = res[seasons[0]], res[seasons[1]]
            rows.append({
                "threshold": thr, "market": market,
                "window": f"{mn}-{mx}",
                **{f"n_{s.replace('/','_').replace('20','')}": r["n"] for s, r in res.items()},
                **{f"win_{s.replace('/','_').replace('20','')}": r["win_rate"] for s, r in res.items()},
                **{f"n_priced_{s.replace('/','_').replace('20','')}": r["n_priced"] for s, r in res.items()},
                **{f"ev_{s.replace('/','_').replace('20','')}": r["ev"] for s, r in res.items()},
            })
        df = pd.DataFrame(rows)
        # Per-league CSV — sort by min(EV) across both seasons, where both n_priced >= floor
        df["ev_min"] = df[["ev_24_25", "ev_25_26"]].min(axis=1, skipna=False)
        path = out_dir / f"{league_short}.csv"
        df.sort_values("ev_min", ascending=False, na_position="last").to_csv(path, index=False)
        print(f"  wrote {path.name}  ({len(df)} rows)")

        # Robust candidates: priced ≥ 30 in BOTH seasons, ev_min ≥ floor
        robust = df[(df["n_priced_24_25"] >= MIN_ROBUST_N_PRICED_PER_SEASON)
                  & (df["n_priced_25_26"] >= MIN_ROBUST_N_PRICED_PER_SEASON)
                  & (df["ev_min"] >= MIN_ROBUST_EV)]
        top = robust.sort_values("ev_min", ascending=False).head(1)
        if not top.empty:
            r = top.iloc[0].to_dict()
            r["league"] = league_short
            summary_rows.append(r)

    if summary_rows:
        sum_df = pd.DataFrame(summary_rows)
        cols = (["league", "threshold", "market", "window",
                 "n_24_25", "win_24_25", "n_priced_24_25", "ev_24_25",
                 "n_25_26", "win_25_26", "n_priced_25_26", "ev_25_26", "ev_min"])
        sum_df = sum_df[[c for c in cols if c in sum_df.columns]]
        out = ROOT / "results" / "per_league_best.csv"
        sum_df.to_csv(out, index=False)
        print()
        print("=== robust per-league candidates ===")
        with pd.option_context("display.width", 220):
            print(sum_df.to_string(index=False))
        print()
        print(f"wrote {out}")
    else:
        print("No league cleared the robustness filter "
              f"(EV ≥ {MIN_ROBUST_EV} AND n_priced ≥ {MIN_ROBUST_N_PRICED_PER_SEASON} both seasons).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
