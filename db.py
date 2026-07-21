"""
Thin SQLite wrapper for the Big Screen Moment backend.

Kept deliberately simple (stdlib sqlite3, no ORM) since this is a pilot
that needs to be easy to read, run, and swap out later - not a
production-scale system yet.
"""

import sqlite3

DB_PATH = "bsm.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL mode lets reads and writes happen at the same time instead of
    # blocking each other - without it, two uploads landing close together
    # (very normal once real clips are flowing in) can throw "database is
    # locked" errors like the one that just happened.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            venue TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS periods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL REFERENCES events(id),
            label TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT
        );

        CREATE TABLE IF NOT EXISTS clips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL REFERENCES events(id),
            period_id INTEGER REFERENCES periods(id),
            timestamp TEXT NOT NULL,
            duration REAL,
            thumbnail_path TEXT,
            video_path TEXT,
            status TEXT NOT NULL DEFAULT 'candidate',
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()
