"""Out-of-sample test: re-apply the 2025/26 winning strategies to 2024/25.

For each strategy (threshold, market, min_minute, max_minute) the in-sample
backtest flagged as a candidate, count triggers + win-rate on the 2024/25
signal subset and compare to 2025/26.

No 2024/25 Betfair odds are loaded, so we report **win rates only** — that's
the right metric for "does the signal generalise?". EV figures from the
in-sample run depend on price data and are reported for the 25/26 column only.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from db.connection import get_conn  # noqa: E402


_MARKET_ODDS_COL = {
    "over_0.5": "betfair_over05_odds",
    "over_1.5": "betfair_over15_odds",
    "over_2.5": "betfair_over25_odds",
    "over_3.5": "betfair_over35_odds",
}


def load_signals_with_season() -> pd.DataFrame:
    conn = get_conn()
    try:
        return pd.read_sql_query(
            """
            SELECT s.*, m.season, m.total_goals AS final_total_goals
            FROM signals s
            JOIN matches m ON m.match_id = s.match_id
            WHERE s.signal_type = 'xg_rate_15m'
            """,
            conn,
        )
    finally:
        conn.close()


def _adjusted_odds(raw: pd.Series) -> pd.Series:
    comm = 1.0 - config.BETFAIR_COMMISSION
    net = (raw - 1.0) * comm + 1.0
    return 1.0 + (net - 1.0) * config.ODDS_HAIRCUT


def evaluate(triggers: pd.DataFrame, market: str) -> dict:
    line = float(market.split("_", 1)[1])
    floor_line = math.floor(line)
    sub = triggers[triggers["goals_total_at_trigger"] == floor_line]
    n = len(sub)
    if n == 0:
        return {"n": 0, "win_rate": float("nan"),
                "n_priced": 0, "win_rate_priced": float("nan"),
                "avg_odds": float("nan"), "ev": float("nan")}
    wins = (sub["final_total_goals"] > line).astype(int)
    out = {"n": n, "win_rate": round(float(wins.mean()), 4),
           "n_priced": 0, "win_rate_priced": float("nan"),
           "avg_odds": float("nan"), "ev": float("nan")}

    odds_col = _MARKET_ODDS_COL[market]
    priced = sub[sub[odds_col].notna()].copy()
    if len(priced):
        priced_wins = (priced["final_total_goals"] > line).astype(int)
        adj = _adjusted_odds(priced[odds_col])
        profit = pd.Series(
            [(a - 1.0) if w == 1 else -1.0 for a, w in zip(adj, priced_wins)],
        )
        out["n_priced"] = len(priced)
        out["win_rate_priced"] = round(float(priced_wins.mean()), 4)
        out["avg_odds"] = round(float(priced[odds_col].mean()), 3)
        out["ev"] = round(float(profit.mean()), 4)
    return out


def main() -> int:
    candidates = pd.read_csv(ROOT / "results" / "best_signals.csv")
    signals = load_signals_with_season()

    by_season = {
        "2025/2026": signals[signals["season"] == "2025/2026"],
        "2024/2025": signals[signals["season"] == "2024/2025"],
    }
    print(f"in-sample (25/26) signals: {len(by_season['2025/2026'])}")
    print(f"out-of-sample (24/25) signals: {len(by_season['2024/2025'])}")
    print()

    out_rows = []
    for _, row in candidates.iterrows():
        thr, mkt, mn, mx = row["threshold"], row["market"], row["min_minute"], row["max_minute"]
        results = {}
        for season, sigs in by_season.items():
            sel = sigs[
                (sigs["signal_value"] >= thr)
                & (sigs["trigger_minute"] >= mn)
                & (sigs["trigger_minute"] <= mx)
            ]
            results[season] = evaluate(sel, mkt)
        a, b = results["2025/2026"], results["2024/2025"]
        out_rows.append({
            "threshold": thr,
            "market": mkt,
            "window": f"{int(mn)}-{int(mx)}",
            "n_25_26": a["n"],
            "win_25_26": a["win_rate"],
            "n_24_25": b["n"],
            "win_24_25": b["win_rate"],
            "n_priced_25_26": a["n_priced"],
            "ev_25_26": a["ev"],
            "n_priced_24_25": b["n_priced"],
            "ev_24_25": b["ev"],
            "ev_delta": (
                b["ev"] - a["ev"] if not (math.isnan(a["ev"]) or math.isnan(b["ev"])) else float("nan")
            ),
        })

    df = pd.DataFrame(out_rows).sort_values("n_priced_25_26", ascending=False)
    out = ROOT / "results" / "holdout_2425.csv"
    df.to_csv(out, index=False)

    with pd.option_context("display.width", 200, "display.max_columns", 12):
        print(df.to_string(index=False))
    print()
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
