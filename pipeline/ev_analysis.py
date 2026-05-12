"""EV analysis: join xG signals to Betfair in-play prices, compute EV, write results.

Two modes:
  * No Betfair files in config.BETFAIR_DATA_DIR  -> "no-odds mode": re-emit the
    backtest win-rate table and tell the user to drop price files in.
  * Files present -> parse them, match events to FotMob matches by team names +
    date, write the over-goals price nearest each trigger into the signals table,
    re-run the backtest (now odds-aware), and write best_signals.csv +
    equity_curve.png for the top strategies.

The event<->match time alignment is best-effort and clearly approximate; treat
EV figures from the odds path as indicative until validated against real files.
"""

import os
from datetime import datetime, timedelta, timezone

import pandas as pd

import config
from db.connection import get_conn
from pipeline.backtest import market_line, run_backtest
from scrapers import betfair_historical as bf
from utils.logging import get_logger
from utils.teams import canonical

logger = get_logger("ev")


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def _parse_dt(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):  # epoch millis or seconds
        v = float(value)
        if v > 1e12:
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc)
    s = str(value).strip().replace("Z", "+00:00")
    for fmt in (None,):  # try fromisoformat first
        try:
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            break
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S",
                "%Y-%m-%d", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Betfair -> signals odds writeback
# --------------------------------------------------------------------------- #
def _load_matches(conn) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT match_id, date, home_team, away_team FROM matches", conn
    )


def _build_event_index(conn) -> dict:
    """(canon_home, canon_away, date) -> match_id."""
    matches = _load_matches(conn)
    index: dict[tuple, str] = {}
    for _, m in matches.iterrows():
        dt = _parse_dt(m["date"])
        day = dt.date().isoformat() if dt else None
        key = (canonical(str(m["home_team"] or "")), canonical(str(m["away_team"] or "")), day)
        index[key] = m["match_id"]
    return index, {m["match_id"]: _parse_dt(m["date"]) for _, m in matches.iterrows()}


def attach_betfair_odds(conn) -> int:
    files = bf.list_files()
    if not files:
        return 0
    event_index, kickoff_by_match = _build_event_index(conn)

    # match_id -> {odds_col -> chosen price}
    chosen: dict[str, dict[str, float]] = {}
    for filepath in files:
        rows = bf.extract_over_goals_prices(filepath)
        # Group by (home, away, line)
        grouped: dict[tuple, list[dict]] = {}
        for r in rows:
            if not r["home"] or not r["away"] or r["last_price_traded"] is None:
                continue
            grouped.setdefault((r["home"], r["away"], r["line"]), []).append(r)
        for (home, away, line), snaps in grouped.items():
            # Try to find the FotMob match: look across any date that matches names.
            match_id = None
            for (h, a, _day), mid in event_index.items():
                if h == home and a == away:
                    match_id = mid
                    break
            if match_id is None:
                continue
            odds_col = bf.LINE_TO_ODDS_COL.get(line)
            if not odds_col:
                continue
            # Pick the price ~ at the median in-play timestamp (proxy for "mid-match").
            timed = sorted(
                ((_parse_dt(s["market_time"]), s["last_price_traded"]) for s in snaps if _parse_dt(s["market_time"])),
            )
            if timed:
                price = timed[len(timed) // 2][1]
            else:
                price = snaps[len(snaps) // 2]["last_price_traded"]
            chosen.setdefault(match_id, {})[odds_col] = float(price)

    if not chosen:
        logger.warning("No Betfair events matched any FotMob match.")
        return 0

    updated = 0
    for match_id, cols in chosen.items():
        set_clause = ", ".join(f"{c} = ?" for c in cols)
        params = list(cols.values()) + [match_id]
        cur = conn.execute(f"UPDATE signals SET {set_clause} WHERE match_id = ?", params)
        updated += cur.rowcount
    conn.commit()
    logger.info("Attached Betfair odds to %d signal rows across %d matches", updated, len(chosen))
    return updated


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
def _write_best(df: pd.DataFrame) -> pd.DataFrame:
    if df["EV"].notna().any():
        best = df[(df["EV"] > config.MIN_EV) & (df["n_triggers"] >= config.MIN_TRIGGERS)]
    else:
        best = df[df["n_triggers"] >= config.MIN_TRIGGERS].head(20)
    path = os.path.join(config.RESULTS_DIR, "best_signals.csv")
    best.to_csv(path, index=False)
    logger.info("Wrote %s (%d rows)", path, len(best))
    return best


def _equity_curve(conn, best: pd.DataFrame) -> None:
    if best.empty or not best["EV"].notna().any():
        logger.info("Skipping equity curve (no priced strategies).")
        return
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        logger.warning("matplotlib not installed; skipping equity_curve.png "
                       "(pip install matplotlib to enable).")
        return
    from pipeline.backtest import load_signals, _adjusted_odds, _win_series, _MARKET_ODDS_COL
    import math

    signals = load_signals(conn)
    fig, ax = plt.subplots(figsize=(10, 6))
    for _, strat in best.head(3).iterrows():
        line = market_line(strat["market"])
        floor_line = math.floor(line)
        sel = signals[
            (signals["signal_value"] >= strat["threshold"])
            & (signals["trigger_minute"] >= strat["min_minute"])
            & (signals["trigger_minute"] <= strat["max_minute"])
            & (signals["goals_total_at_trigger"] == floor_line)
        ].sort_values("trigger_minute")
        odds_col = _MARKET_ODDS_COL[strat["market"]]
        sel = sel[sel[odds_col].notna()]
        if sel.empty:
            continue
        wins = _win_series(sel, line, config.DEFAULT_GOAL_WINDOW)
        adj = _adjusted_odds(sel[odds_col]).to_numpy()
        profit = (wins.to_numpy() * adj - 1.0)
        ax.plot(range(1, len(profit) + 1), profit.cumsum(),
                label=f"{strat['market']} thr={strat['threshold']} {strat['min_minute']}-{strat['max_minute']}m")
    ax.axhline(0, color="grey", lw=0.8)
    ax.set_xlabel("bet #")
    ax.set_ylabel("cumulative P&L (1u stake)")
    ax.set_title("Equity curve — top strategies")
    ax.legend()
    path = os.path.join(config.RESULTS_DIR, "equity_curve.png")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    logger.info("Wrote %s", path)


def _print_summary(df: pd.DataFrame, priced: bool) -> None:
    print("\n=== xG signal backtest summary ===")
    if df.empty:
        print("No results — did you run `ingest` and `signals` first?")
        return
    top = df.iloc[0]
    print(f"Best row: signal={top['signal_type']} threshold={top['threshold']} "
          f"market={top['market']} window={top['min_minute']}-{top['max_minute']}min")
    print(f"  n_triggers={top['n_triggers']}  win_rate={top['win_rate']}")
    if priced:
        print(f"  avg_odds={top['avg_odds']}  EV={top['EV']}  "
              f"total_profit(1u)={top['total_profit_1unit_stake']}  sharpe={top['sharpe']}")
        print("  Recommended minimum odds to enter: implied break-even = "
              f"{round(1.0 / top['win_rate'], 3) if top['win_rate'] else 'n/a'}")
    else:
        print("  (EV columns are placeholders — no Betfair files found.)")


def run_ev_analysis(conn=None) -> pd.DataFrame:
    own = conn is None
    if own:
        conn = get_conn()
    try:
        os.makedirs(config.RESULTS_DIR, exist_ok=True)
        files = bf.list_files()
        priced = False
        if files:
            n = attach_betfair_odds(conn)
            priced = n > 0
        else:
            print(f"No Betfair files in {config.BETFAIR_DATA_DIR} — running in no-odds mode.\n"
                  "Drop historical price files there and re-run `ev` for full EV figures.")
        df = run_backtest(conn, write=True)
        best = _write_best(df)
        _equity_curve(conn, best)
        _print_summary(df, priced)
        return df
    finally:
        if own:
            conn.close()


if __name__ == "__main__":  # pragma: no cover
    run_ev_analysis()
