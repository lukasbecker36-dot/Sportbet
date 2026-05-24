"""Build a per-(league, line, goals_total, minute_bucket) median Betfair LTP.

Output: data/synthetic_odds_lookup.csv — loaded at bot startup as the
in-play odds gate. Replaces the SofaScore odds gate, which proved
unreliable (bookmaker quotes lag the Exchange by minutes in-play).

Methodology:
  For each historical priced signal in our 7-league dataset, key by
    (league_short, market_line, goals_total_at_trigger, minute_bucket)
  and aggregate the betfair_overXX_odds values. Minute buckets are
  10-min wide ([30-40), [40-50), ..., [80-90)). Each bucket gets
  count + median + 25th/75th percentile. We also emit an 'all'
  bucket per (league, line, goals_total) as a fallback when the
  exact bucket has too few samples.

Loaded by live/odds_lookup.py; consumed by the alert-only gate in
live/runner.py.
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

LEAGUE_DB_NAME = {
    "PL":  "ENG-Premier League",
    "ESP": "ESP-La Liga",
    "ITA": "ITA-Serie A",
    "GER": "GER-Bundesliga",
    "FRA": "FRA-Ligue 1",
    "MLS": "USA-Major League Soccer",
    "BRA": "BRA-Serie A",
}

# Minute buckets — 10 min wide, covering the active in-play firing window.
BUCKETS = [(30, 40), (40, 50), (50, 60), (60, 70), (70, 80), (80, 90)]

MARKET_ODDS_COL = {
    0.5: "betfair_over05_odds",
    1.5: "betfair_over15_odds",
    2.5: "betfair_over25_odds",
    3.5: "betfair_over35_odds",
}


def main() -> int:
    conn = get_conn()
    try:
        df = pd.read_sql_query(
            """
            SELECT s.trigger_minute, s.goals_total_at_trigger,
                   s.betfair_over05_odds, s.betfair_over15_odds,
                   s.betfair_over25_odds, s.betfair_over35_odds,
                   m.league
            FROM signals s JOIN matches m ON m.match_id = s.match_id
            WHERE s.signal_type = 'xg_rate_15m'
            """,
            conn,
        )
    finally:
        conn.close()

    rows = []
    for short, full in LEAGUE_DB_NAME.items():
        lsig = df[df["league"] == full]
        if lsig.empty:
            continue
        for line, odds_col in MARKET_ODDS_COL.items():
            floor_line = math.floor(line)
            sub = lsig[(lsig["goals_total_at_trigger"] == floor_line)
                       & (lsig[odds_col].notna())]
            if sub.empty:
                continue
            for (lo, hi) in BUCKETS:
                b = sub[(sub["trigger_minute"] >= lo) & (sub["trigger_minute"] < hi)]
                if b.empty:
                    continue
                rows.append({
                    "league": short, "line": line,
                    "goals_total": floor_line,
                    "minute_bucket": f"{lo}-{hi}",
                    "min_min": lo, "max_min": hi,
                    "n": int(len(b)),
                    "median_odds": round(float(b[odds_col].median()), 3),
                    "p25_odds": round(float(b[odds_col].quantile(0.25)), 3),
                    "p75_odds": round(float(b[odds_col].quantile(0.75)), 3),
                })
            # 'all' fallback for this (league, line, goals_total)
            rows.append({
                "league": short, "line": line,
                "goals_total": floor_line,
                "minute_bucket": "all",
                "min_min": 0, "max_min": 999,
                "n": int(len(sub)),
                "median_odds": round(float(sub[odds_col].median()), 3),
                "p25_odds": round(float(sub[odds_col].quantile(0.25)), 3),
                "p75_odds": round(float(sub[odds_col].quantile(0.75)), 3),
            })

    out_df = pd.DataFrame(rows)
    out_path = ROOT / "data" / "synthetic_odds_lookup.csv"
    out_df.to_csv(out_path, index=False)
    print(f"wrote {out_path}  ({len(out_df)} rows)")
    print()
    # Print a per-league summary so we can sanity-check
    for short in LEAGUE_DB_NAME:
        sub = out_df[(out_df["league"] == short) & (out_df["minute_bucket"] != "all")]
        if sub.empty:
            print(f"{short}: no priced data")
            continue
        print(f"=== {short} ===")
        print(sub[["line", "goals_total", "minute_bucket",
                   "n", "median_odds", "p25_odds", "p75_odds"]].to_string(index=False))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
