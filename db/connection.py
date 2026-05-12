"""SQLite connection helpers."""

import os
import sqlite3

import config

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def get_conn(db_path: str | None = None) -> sqlite3.Connection:
    db_path = db_path or config.DB_PATH
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        conn.executescript(fh.read())
    conn.commit()
