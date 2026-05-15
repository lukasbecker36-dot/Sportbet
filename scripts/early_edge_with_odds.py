"""Full 15-55 minute early-edge backtest with real Betfair-attached odds.

The signals table normally only stores crossings from minute 30+ (the floor
of config.MIN_MINUTE). To capture the 15-29 portion we monkey-patch the floor
in-memory, rerun populate_signals so the new earlier rows get inserted, then
run ev_analysis to attach Betfair odds to every signal (old + new).

After this script finishes the signals table contains EXTRA early rows that
older analyses still ignore (they filter by trigger_minute >= 30). Nothing is
destroyed; re-running with the default config will simply recompute back to
the same state.
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

# Make sure the floor sees minute 15 — monkey-patch in-memory; no file write.
if 15 not in config.MIN_MINUTE:
    config.MIN_MINUTE = sorted({15, *config.MIN_MINUTE})
    print(f"patched config.MIN_MINUTE -> {config.MIN_MINUTE}")

from db.connection import get_conn  # noqa: E402
from pipeline.signals import populate_signals  # noqa: E402
from pipeline.ev_analysis import run_ev_analysis  # noqa: E402


def main() -> int:
    print("[1/3] recomputing signals with min_minute floor = 15 …")
    populate_signals()

    print("[2/3] re-attaching Betfair odds (this re-parses ~14k bz2 files; ~2 min) …")
    run_ev_analysis()

    print("[3/3] running early-edge analysis on PL …")
    conn = get_conn()
    sigs = pd.read_sql_query(
        """
        SELECT s.*, m.season, m.total_goals AS final_total
        FROM signals s JOIN matches m ON m.match_id = s.match_id
        WHERE m.league='ENG-Premier League' AND s.signal_type='xg_rate_15m'
        """,
        conn,
    )
    conn.close()

    ODDS = {0: "betfair_over15_odds", 1: "betfair_over25_odds", 2: "betfair_over35_odds"}

    def adj_back(o: float) -> float:
        n = (o - 1.0) * (1.0 - config.BETFAIR_COMMISSION) + 1.0
        return 1.0 + (n - 1.0) * config.ODDS_HAIRCUT

    rows = []
    for thr in [0.20, 0.25, 0.30, 0.35, 0.40, 0.50]:
        for window in [(15, 29), (15, 45), (15, 55), (30, 55)]:
            mn, mx = window
            sub = sigs[
                (sigs.signal_value >= thr)
                & (sigs.trigger_minute >= mn)
                & (sigs.trigger_minute <= mx)
                & (sigs.goals_total_at_trigger.isin([0, 1, 2]))
            ].copy()
            sub["line"] = sub.goals_total_at_trigger + 1.5
            sub["odds"] = sub.apply(
                lambda r: r[ODDS[int(r.goals_total_at_trigger)]], axis=1,
            )
            for season_label, slice_df in [
                ("ALL", sub),
                ("2024/2025", sub[sub.season == "2024/2025"]),
                ("2025/2026", sub[sub.season == "2025/2026"]),
            ]:
                priced = slice_df[slice_df.odds.notna()].copy()
                if len(priced) < 10:
                    continue
                priced["won"] = (priced.final_total > priced.line).astype(int)
                priced["adj"] = priced.odds.apply(adj_back)
                priced["profit"] = np.where(priced.won == 1, priced.adj - 1.0, -1.0)
                rows.append({
                    "thr": thr,
                    "window": f"{mn}-{mx}",
                    "season": season_label,
                    "n_priced": int(len(priced)),
                    "win_rate": round(float(priced.won.mean()), 4),
                    "avg_odds": round(float(priced.odds.mean()), 3),
                    "ev": round(float(priced.profit.mean()), 4),
                    "profit_£10": int(round(priced.profit.sum() * 10)),
                })
    df = pd.DataFrame(rows)
    out = ROOT / "results" / "early_edge_pl_with_odds.csv"
    df.to_csv(out, index=False)

    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 200)
    print()
    print("Early-edge PL (line = goals_at_trigger + 1.5, win = final_total > line):")
    print(df.to_string(index=False))
    print()
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
