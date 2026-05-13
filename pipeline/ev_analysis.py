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
# Betfair -> signals odds writeback (per-signal, time-accurate)
# --------------------------------------------------------------------------- #
_PRICE_MIN, _PRICE_MAX = 1.01, 100.0


def _load_matches(conn) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT match_id, date, home_team, away_team FROM matches", conn
    )


def _build_event_index(conn) -> dict[tuple, str]:
    """(canon_home, canon_away, YYYY-MM-DD) -> match_id."""
    matches = _load_matches(conn)
    index: dict[tuple, str] = {}
    for _, m in matches.iterrows():
        dt = _parse_dt(m["date"])
        day = dt.date().isoformat() if dt else None
        key = (canonical(str(m["home_team"] or "")), canonical(str(m["away_team"] or "")), day)
        index[key] = m["match_id"]
    return index


def _match_minute_to_offset(minute: int) -> timedelta:
    """Wall-clock elapsed since kickoff for a given *match* minute.

    Adds a ~15-minute lump for the half-time break once past minute 45.
    """
    minute = max(int(minute), 0)
    extra = 15 if minute > 45 else 0
    return timedelta(minutes=minute + extra)


def _price_at(series: list[tuple], target: datetime) -> float | None:
    """Last in-range traded price at-or-before ``target`` (series is time-sorted)."""
    chosen = None
    for ts, price in series:
        if ts is None:
            continue
        if ts > target:
            break
        if _PRICE_MIN <= price <= _PRICE_MAX:
            chosen = price
    if chosen is None:  # nothing valid before kickoff+offset — fall back to first valid
        for ts, price in series:
            if _PRICE_MIN <= price <= _PRICE_MAX:
                return price
    return chosen


def attach_betfair_odds(conn) -> int:
    files = bf.list_files()
    if not files:
        return 0
    # Recompute from scratch every run — clear any previously written odds.
    conn.execute(
        "UPDATE signals SET betfair_over05_odds = NULL, betfair_over15_odds = NULL, "
        "betfair_over25_odds = NULL, betfair_over35_odds = NULL"
    )
    event_index = _build_event_index(conn)

    # match_id -> {line -> [(ts, price), ...]}  and  match_id -> kickoff datetime
    series_by_match: dict[str, dict[float, list[tuple]]] = {}
    kickoff_by_match: dict[str, datetime] = {}

    for filepath in files:
        market = bf.parse_market(filepath)
        if not market or not market["home"] or not market["away"]:
            continue
        kickoff = _parse_dt(market["kickoff"])
        snaps = [(_parse_dt(ts), p) for ts, p in market["snapshots"]]
        snaps = [(ts, p) for ts, p in snaps if ts is not None]
        if not snaps:
            continue
        snaps.sort(key=lambda t: t[0])
        if kickoff is None:
            kickoff = snaps[0][0]

        # Match on (home, away, day) with a +/- 1 day tolerance (UTC vs local).
        match_id = None
        for delta in (0, -1, 1):
            day = (kickoff + timedelta(days=delta)).date().isoformat()
            mid = event_index.get((market["home"], market["away"], day))
            if mid:
                match_id = mid
                break
        if match_id is None:
            continue

        line = market["line"]
        series_by_match.setdefault(match_id, {}).setdefault(line, []).extend(snaps)
        # Prefer the first kickoff we see for a match (markets agree on it anyway).
        kickoff_by_match.setdefault(match_id, kickoff)

    if not series_by_match:
        logger.warning("No Betfair events matched any FotMob match.")
        return 0

    for by_line in series_by_match.values():
        for series in by_line.values():
            series.sort(key=lambda t: t[0])

    signals = pd.read_sql_query(
        "SELECT id, match_id, trigger_minute FROM signals", conn
    )
    updated = 0
    matched_signal_matches: set[str] = set()
    for _, sig in signals.iterrows():
        match_id = sig["match_id"]
        by_line = series_by_match.get(match_id)
        if not by_line:
            continue
        kickoff = kickoff_by_match[match_id]
        target = kickoff + _match_minute_to_offset(sig["trigger_minute"])
        cols: dict[str, float] = {}
        for line, series in by_line.items():
            odds_col = bf.LINE_TO_ODDS_COL.get(line)
            if not odds_col:
                continue
            price = _price_at(series, target)
            if price is not None:
                cols[odds_col] = float(price)
        if not cols:
            continue
        set_clause = ", ".join(f"{c} = ?" for c in cols)
        params = list(cols.values()) + [int(sig["id"])]
        conn.execute(f"UPDATE signals SET {set_clause} WHERE id = ?", params)
        updated += 1
        matched_signal_matches.add(match_id)
    conn.commit()
    logger.info(
        "Attached time-accurate Betfair odds to %d signal rows across %d matches",
        updated, len(matched_signal_matches),
    )
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
        wins = _win_series(sel, line)
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
