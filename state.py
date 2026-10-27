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
    edition_date TEXT,         -- date it appeared, if shown
    title_key TEXT             -- normalized title, for cross-day dedupe
);
CREATE TABLE IF NOT EXISTS editions (
    date TEXT PRIMARY KEY,
    path TEXT,
    item_count INTEGER,
    created_at TEXT,
    status TEXT,               -- 'published' | 'failed' | 'noop'
    run_id TEXT                -- execution that produced this row
);
CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY,
    date TEXT,
    source TEXT,
    kind TEXT,                 -- 'feed' | 'search'
    items INTEGER,
    ok INTEGER,                -- 1 ok, 0 failed
    error TEXT,
    run_id TEXT                -- execution that produced this row
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


def _migrate(conn):
    """Add columns that newer code expects to a database created earlier.

    The committed database evolves with the code; a fresh CREATE TABLE
    does not alter existing tables, so each new column is added here
    once and backfilled where a value can be derived.
    """
    def columns(table):
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

    if "title_key" not in columns("candidates"):
        conn.execute("ALTER TABLE candidates ADD COLUMN title_key TEXT")
    if "run_id" not in columns("editions"):
        conn.execute("ALTER TABLE editions ADD COLUMN run_id TEXT")
    if "run_id" not in columns("source_runs"):
        conn.execute("ALTER TABLE source_runs ADD COLUMN run_id TEXT")
    conn.commit()

    # Backfill normalized titles for rows collected before title_key existed.
    missing = conn.execute(
        "SELECT id, title FROM candidates WHERE title_key IS NULL OR title_key = ''"
    ).fetchall()
    if missing:
        from rank import normalize_title  # local import: rank imports state lazily
        for row in missing:
            conn.execute("UPDATE candidates SET title_key = ? WHERE id = ?",
                         (normalize_title(row["title"]), row["id"]))
        conn.commit()


def connect(db_path):
    """Open (and create if needed) the state database."""
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)
    return conn


def _title_key(cand):
    """Normalized title for dedupe; computed here so upserts stay consistent."""
    from rank import normalize_title  # local import: rank imports state lazily
    return normalize_title(cand.get("title", ""))


def upsert_candidate(conn, cand):
    """Insert a candidate, or ignore it if the URL is already known."""
    conn.execute(
        """INSERT OR IGNORE INTO candidates
           (url, title, summary, source, source_type, section,
            published, fetched_at, score, keywords, matched_companies, status,
            title_key)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)""",
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
            _title_key(cand),
        ),
    )


def get_new_candidates(conn):
    """Candidates not yet shown or skipped, newest first."""
    rows = conn.execute(
        "SELECT * FROM candidates WHERE status = 'new' ORDER BY published DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def mark_shown(conn, candidate_ids, edition_date, commit=True):
    if not candidate_ids:
        return
    conn.execute(
        "UPDATE candidates SET status = 'shown', edition_date = ? "
        "WHERE id IN (%s)" % ",".join("?" * len(candidate_ids)),
        [edition_date] + list(candidate_ids),
    )
    if commit:
        conn.commit()


def mark_skipped(conn, candidate_ids, commit=True):
    if not candidate_ids:
        return
    conn.execute(
        "UPDATE candidates SET status = 'skipped' "
        "WHERE id IN (%s)" % ",".join("?" * len(candidate_ids)),
        list(candidate_ids),
    )
    if commit:
        conn.commit()


def record_published_edition(conn, candidate_ids, date, path, item_count,
                             created_at, run_id=""):
    """Mark an edition's stories shown AND record the edition, in one commit.

    The two updates used to commit separately: if the second failed, the
    stories were marked shown with an edition_date but no edition receipt
    existed, and a rerun could publish a different edition over the same
    date. One transaction closes that window.
    """
    mark_shown(conn, candidate_ids, date, commit=False)
    record_edition(conn, date, path, item_count, created_at, "published",
                   run_id=run_id, commit=False)
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


def get_edition_candidates(conn, date):
    """Stories recorded as shown in one edition, in pick order.

    Used by --force rebuilds: the edition is re-rendered from exactly
    these stories instead of re-ranking. JSON list fields are parsed
    back to lists so rendering matches the in-memory shape.
    """
    rows = conn.execute(
        "SELECT * FROM candidates WHERE status = 'shown' AND edition_date = ? "
        "ORDER BY score DESC, published DESC",
        (date,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for field in ("keywords", "matched_companies"):
            try:
                d[field] = json.loads(d.get(field) or "[]")
            except (TypeError, ValueError):
                d[field] = []
        out.append(d)
    return out


def get_candidate_by_url(conn, url):
    row = conn.execute("SELECT * FROM candidates WHERE url = ?", (url,)).fetchone()
    if not row:
        return None
    d = dict(row)
    for field in ("keywords", "matched_companies"):
        try:
            d[field] = json.loads(d.get(field) or "[]")
        except (TypeError, ValueError):
            d[field] = []
    return d


def get_recent_title_keys(conn, since_date):
    """Normalized titles shown in editions on or after since_date.

    Cross-day dedupe: a story whose normalized title already appeared
    recently is not published again under a different URL.
    """
    rows = conn.execute(
        "SELECT DISTINCT title_key FROM candidates "
        "WHERE status = 'shown' AND edition_date >= ? "
        "AND title_key IS NOT NULL AND title_key != ''",
        (since_date,),
    ).fetchall()
    return {r["title_key"] for r in rows}


def record_edition(conn, date, path, item_count, created_at, status,
                   run_id="", commit=True):
    conn.execute(
        """INSERT OR REPLACE INTO editions
           (date, path, item_count, created_at, status, run_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (date, path, item_count, created_at, status, run_id),
    )
    if commit:
        conn.commit()


def record_source_run(conn, date, source, kind, items, ok, error="",
                      run_id=""):
    conn.execute(
        """INSERT INTO source_runs (date, source, kind, items, ok, error, run_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (date, source, kind, items, 1 if ok else 0, error, run_id),
    )
    conn.commit()


def get_source_runs(conn, date, run_id=None):
    """Source runs for a date; pass run_id to see one execution only.

    Without run_id a same-day rerun's rows mix with the first run's,
    so a published edition could list a failure that a later retry fixed.
    """
    if run_id:
        rows = conn.execute(
            "SELECT * FROM source_runs WHERE date = ? AND run_id = ? "
            "ORDER BY kind, source",
            (date, run_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM source_runs WHERE date = ? ORDER BY kind, source",
            (date,),
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
