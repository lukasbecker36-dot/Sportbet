"""Grid-search backtest over xG-signal thresholds, entry windows and markets.

Win modelling (the crux): a trigger at minute m with G goals already scored is
evaluated against the over-(G+0.5) line only — the live-relevant case. It "wins"
if the match's final total exceeds that line, i.e. at least one more goal is
scored at any point before full time (this is how the Betfair over-goals market
actually settles if you hold the bet). Markets where G != floor(line) are skipped.

Betfair odds are absent in this build, so avg_odds / EV / total_profit / sharpe
are NaN here; pipeline.ev_analysis fills them once price files are available.
"""

import math

import numpy as np
import pandas as pd

import config
from db.connection import get_conn
from utils.logging import get_logger

logger = get_logger("backtest")

_RESULT_COLS = [
    "signal_type", "threshold", "market", "min_minute", "max_minute",
    "n_triggers", "win_rate", "avg_odds", "EV", "total_profit_1unit_stake", "sharpe",
]
_MARKET_ODDS_COL = {
    "over_0.5": "betfair_over05_odds",
    "over_1.5": "betfair_over15_odds",
    "over_2.5": "betfair_over25_odds",
    "over_3.5": "betfair_over35_odds",
}


def market_line(market: str) -> float:
    # 'over_2.5' -> 2.5
    return float(market.split("_", 1)[1])


def load_signals(conn) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT s.*, m.total_goals AS final_total_goals,
               m.home_goals AS final_home_goals, m.away_goals AS final_away_goals
        FROM signals s JOIN matches m ON m.match_id = s.match_id
        WHERE s.signal_type = ?
        """,
        conn, params=("xg_rate_15m",),
    )


def _win_series(triggers: pd.DataFrame, line: float) -> pd.Series:
    """1 if the match's final total goals exceeds ``line`` (hold-to-FT settlement)."""
    return (triggers["final_total_goals"] > line).astype(int)


def _grid():
    for signal_type in ["xg_rate_15m"]:
        for threshold in config.XG_RATE_THRESHOLDS:
            for market in config.MARKETS:
                for min_m in config.MIN_MINUTE:
                    for max_m in config.MAX_MINUTE:
                        if min_m >= max_m:
                            continue
                        yield signal_type, threshold, market, min_m, max_m


def _adjusted_odds(raw: pd.Series) -> pd.Series:
    comm = 1.0 - config.BETFAIR_COMMISSION
    net = (raw - 1.0) * comm + 1.0
    # Conservative in-play haircut on the edge.
    return 1.0 + (net - 1.0) * config.ODDS_HAIRCUT


def _evaluate(triggers: pd.DataFrame, market: str) -> dict:
    line = market_line(market)
    floor_line = math.floor(line)
    sub = triggers[triggers["goals_total_at_trigger"] == floor_line]
    n = len(sub)
    out = {"n_triggers": n, "win_rate": np.nan, "avg_odds": np.nan,
           "EV": np.nan, "total_profit_1unit_stake": np.nan, "sharpe": np.nan}
    if n == 0:
        return out
    wins = _win_series(sub, line)
    out["win_rate"] = round(float(wins.mean()), 4)

    odds_col = _MARKET_ODDS_COL[market]
    if odds_col in sub.columns and sub[odds_col].notna().any():
        priced = sub[sub[odds_col].notna()].copy()
        priced_wins = _win_series(priced, line)
        adj = _adjusted_odds(priced[odds_col])
        profit = np.where(priced_wins == 1, adj - 1.0, -1.0)
        out["n_triggers"] = len(priced)
        out["win_rate"] = round(float(priced_wins.mean()), 4)
        out["avg_odds"] = round(float(priced[odds_col].mean()), 3)
        out["EV"] = round(float(profit.mean()), 4)
        out["total_profit_1unit_stake"] = round(float(profit.sum()), 3)
        out["sharpe"] = round(float(profit.mean() / profit.std()), 4) if profit.std() > 0 else np.nan
    return out


def run_backtest(conn=None, *, write: bool = True) -> pd.DataFrame:
    own = conn is None
    if own:
        conn = get_conn()
    try:
        signals = load_signals(conn)
        rows = []
        for signal_type, threshold, market, min_m, max_m in _grid():
            sel = signals[
                (signals["signal_value"] >= threshold)
                & (signals["trigger_minute"] >= min_m)
                & (signals["trigger_minute"] <= max_m)
            ]
            stats = _evaluate(sel, market)
            rows.append({
                "signal_type": signal_type, "threshold": threshold, "market": market,
                "min_minute": min_m, "max_minute": max_m, **stats,
            })
        df = pd.DataFrame(rows, columns=_RESULT_COLS)
        sort_key = "EV" if df["EV"].notna().any() else "win_rate"
        df = df.sort_values(sort_key, ascending=False, na_position="last").reset_index(drop=True)
        if write:
            import os
            os.makedirs(config.RESULTS_DIR, exist_ok=True)
            path = os.path.join(config.RESULTS_DIR, "signal_ev_table.csv")
            df.to_csv(path, index=False)
            logger.info("Wrote %s (%d rows)", path, len(df))
        return df
    finally:
        if own:
            conn.close()


if __name__ == "__main__":  # pragma: no cover
    out = run_backtest()
    with pd.option_context("display.max_rows", 20, "display.width", 160):
        print(out.head(20))
