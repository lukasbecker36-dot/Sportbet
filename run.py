#!/usr/bin/env python3
"""Sportbet CLI — FotMob xG signal backtester.

Usage:
    python run.py init-db                    # create the SQLite schema
    python run.py ingest                     # scrape FotMob -> matches + shots tables
    python run.py ingest-statsbomb           # load StatsBomb open-data (La Liga, no login needed)
    python run.py ingest-statsbomb "Champions League"
    python run.py signals                    # compute xG signals -> signals table
    python run.py backtest                   # grid search -> results/signal_ev_table.csv
    python run.py ev                         # join Betfair odds (if any) + write results
    python run.py all                        # init-db -> ingest -> signals -> backtest -> ev
"""

import argparse
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="run.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command",
                        choices=["init-db", "ingest", "ingest-statsbomb",
                                 "signals", "backtest", "ev", "all"])
    parser.add_argument("args", nargs="*", help="extra arguments (e.g. competition name)")
    args = parser.parse_args(argv)

    from db.connection import get_conn, init_db

    if args.command == "init-db":
        conn = get_conn()
        init_db(conn)
        conn.close()
        print("Schema created.")
        return 0

    if args.command == "ingest-statsbomb":
        conn = get_conn()
        init_db(conn)
        from scrapers.statsbomb import ingest_statsbomb
        competition = " ".join(args.args) if args.args else "La Liga"
        ingest_statsbomb(competition, conn)
        conn.close()
        return 0

    if args.command in ("ingest", "all"):
        conn = get_conn()
        init_db(conn)
        from pipeline.ingest import ingest_all
        ingest_all(conn)
        conn.close()
        if args.command == "ingest":
            return 0

    if args.command in ("signals", "all"):
        from pipeline.signals import populate_signals
        populate_signals()
        if args.command == "signals":
            return 0

    if args.command in ("backtest", "all"):
        from pipeline.backtest import run_backtest
        run_backtest()
        if args.command == "backtest":
            return 0

    if args.command in ("ev", "all"):
        from pipeline.ev_analysis import run_ev_analysis
        run_ev_analysis()
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
