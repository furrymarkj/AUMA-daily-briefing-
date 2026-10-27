"""SQLite state for the AUMA daily briefing.

One small database (state/briefing.db) tracks everything the daily run
needs to remember:

- candidates: every story ever collected, with its score and whether it
  has already appeared in an edition (so nothing is shown twice).
- editions: one row per published day — the receipt that makes rerunning
  a completed day a no-op.
- source_runs: per-source results for each day (what worked, what failed).
- feedback / requests: story ratings and change requests (see feedback.py).

The database file is committed back to the repo by the GitHub Action so
delivery history survives between runs. No personal data lives here —
only public story URLs, titles and scores.
"""

import json
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    source TEXT,
    source_type TEXT,          -- 'feed' or 'search'
    section TEXT,              -- 'industry' | 'competitors' | 'customers' | 'wildcard'
    published TEXT,            -- ISO date-time of the story (may be empty)
    fetched_at TEXT,
    score REAL DEFAULT 0,
    keywords TEXT,             -- JSON list of matched keywords
    matched_companies TEXT,    -- JSON list of matched company names
    status TEXT DEFAULT 'new', -- 'new' | 'shown' | 'skipped'
    edition_date TEXT          -- date it appeared, if shown
);
CREATE TABLE IF NOT EXISTS editions (
    date TEXT PRIMARY KEY,
    path TEXT,
    item_count INTEGER,
    created_at TEXT,
    status TEXT                -- 'published' | 'failed' | 'noop'
);
CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY,
    date TEXT,
    source TEXT,
    kind TEXT,                 -- 'feed' | 'search'
    items INTEGER,
    ok INTEGER,                -- 1 ok, 0 failed
    error TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    date TEXT,
    url TEXT,
    rating INTEGER,            -- 1..5
    note TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    text TEXT,
    kind TEXT,                 -- 'add' | 'change' | 'remove' | 'other'
    status TEXT DEFAULT 'open',
    created_at TEXT
);
"""


def connect(db_path):
    """Open (and create if needed) the state database."""
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def upsert_candidate(conn, cand):
    """Insert a candidate, or ignore it if the URL is already known."""
    conn.execute(
        """INSERT OR IGNORE INTO candidates
           (url, title, summary, source, source_type, section,
            published, fetched_at, score, keywords, matched_companies, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')""",
        (
            cand["url"],
            cand["title"],
            cand.get("summary", ""),
            cand.get("source", ""),
            cand.get("source_type", ""),
            cand.get("section", "industry"),
            cand.get("published", ""),
            cand.get("fetched_at", ""),
            cand.get("score", 0),
            json.dumps(cand.get("keywords", [])),
            json.dumps(cand.get("matched_companies", [])),
        ),
    )


def get_new_candidates(conn):
    """Candidates not yet shown or skipped, newest first."""
    rows = conn.execute(
        "SELECT * FROM candidates WHERE status = 'new' ORDER BY published DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def mark_shown(conn, candidate_ids, edition_date):
    conn.execute(
        "UPDATE candidates SET status = 'shown', edition_date = ? "
        "WHERE id IN (%s)" % ",".join("?" * len(candidate_ids)),
        [edition_date] + list(candidate_ids),
    )
    conn.commit()


def mark_skipped(conn, candidate_ids):
    conn.execute(
        "UPDATE candidates SET status = 'skipped' "
        "WHERE id IN (%s)" % ",".join("?" * len(candidate_ids)),
        list(candidate_ids),
    )
    conn.commit()


def update_scored(conn, cand):
    """Persist a candidate's score, matches, and section after ranking."""
    conn.execute(
        """UPDATE candidates
           SET score = ?, keywords = ?, matched_companies = ?, section = ?
           WHERE id = ?""",
        (
            cand.get("score", 0),
            json.dumps(cand.get("keywords", [])),
            json.dumps(cand.get("matched_companies", [])),
            cand.get("section", "industry"),
            cand["id"],
        ),
    )


def get_edition(conn, date):
    row = conn.execute("SELECT * FROM editions WHERE date = ?", (date,)).fetchone()
    return dict(row) if row else None


def record_edition(conn, date, path, item_count, created_at, status):
    conn.execute(
        """INSERT OR REPLACE INTO editions (date, path, item_count, created_at, status)
           VALUES (?, ?, ?, ?, ?)""",
        (date, path, item_count, created_at, status),
    )
    conn.commit()


def record_source_run(conn, date, source, kind, items, ok, error=""):
    conn.execute(
        """INSERT INTO source_runs (date, source, kind, items, ok, error)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (date, source, kind, items, 1 if ok else 0, error),
    )
    conn.commit()


def get_source_runs(conn, date):
    rows = conn.execute(
        "SELECT * FROM source_runs WHERE date = ? ORDER BY kind, source", (date,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_past_edition_dates(conn, limit=30):
    rows = conn.execute(
        "SELECT date FROM editions WHERE status = 'published' "
        "ORDER BY date DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [r["date"] for r in rows]


def add_feedback(conn, date, url, rating, note, created_at):
    conn.execute(
        "INSERT INTO feedback (date, url, rating, note, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (date, url, rating, note, created_at),
    )
    conn.commit()


def add_request(conn, text, kind, created_at):
    conn.execute(
        "INSERT INTO requests (text, kind, created_at) VALUES (?, ?, ?)",
        (text, kind, created_at),
    )
    conn.commit()


def get_feedback(conn, limit=50):
    rows = conn.execute(
        "SELECT * FROM feedback ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_open_requests(conn):
    rows = conn.execute(
        "SELECT * FROM requests WHERE status = 'open' ORDER BY created_at"
    ).fetchall()
    return [dict(r) for r in rows]
