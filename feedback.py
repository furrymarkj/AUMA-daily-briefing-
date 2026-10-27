"""Feedback and the AI-refinement loop for the AUMA daily briefing.

No website, no login, no authenticated endpoint in v1 — just a small
command-line tool that records ratings and change requests:

    python3 feedback.py rate <story-url-or-id> <1-5> [--note "too much oil & gas"]
    python3 feedback.py request "add coverage of data center cooling"
    python3 feedback.py list
    python3 feedback.py packet

`packet` exports a review packet (review_packet.json) with the current
config, recent feedback (each rated story joined with the score,
keywords, matched companies, section, and selection outcome that
produced it), open requests, and recent edition stats. That packet is
the input for the manual AI-refinement loop: give it to an AI
assistant, which proposes changes to keywords, weights, or search
queries. You review every proposal and apply the ones you accept by
editing config/*.json yourself — the tool does not validate or apply
proposals. Nothing is ever rewritten silently because you rated a story.

Storage (round 2, N8): ratings, notes and requests live in feedback.db
— a SEPARATE SQLite database at the repo root, listed in .gitignore,
never committed. The delivery-state database (state/briefing.db) is
public in this repo and must never hold free-text feedback, which can
contain private judgments. The packet joins the two: story evidence
comes from briefing.db (read-only), ratings from feedback.db. Both
files live on this machine; neither is uploaded anywhere by this tool.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "state", "briefing.db")
FEEDBACK_DB_PATH = os.path.join(ROOT, "feedback.db")
PACKET_PATH = os.path.join(ROOT, "review_packet.json")

FEEDBACK_SCHEMA = """
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


def connect_feedback():
    """Open (and create if needed) the private feedback database."""
    conn = sqlite3.connect(FEEDBACK_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(FEEDBACK_SCHEMA)
    conn.commit()
    return conn


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


# No legacy-row copy step: before round 2, feedback.py wrote into
# state/briefing.db, but that storage was verified empty (0 rows) on
# Oct 27, 2026, and state._migrate drops the old tables. If feedback
# rows ever appear in briefing.db again, that is a bug — this module
# is the only feedback writer.

def chicago_today():
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/Chicago")).date().isoformat()


def cmd_rate(delivery_conn, fb_conn, args):
    """Rate a story 1-5. Accepts a URL or a candidate id number."""
    target = args.target
    url = target
    if target.isdigit():
        row = delivery_conn.execute(
            "SELECT url FROM candidates WHERE id = ?", (int(target),)
        ).fetchone()
        if not row:
            print(f"No candidate with id {target}.")
            return 1
        url = row["url"]
    now = datetime.now(timezone.utc).isoformat()
    add_feedback(fb_conn, chicago_today(), url, args.rating, args.note or "", now)
    print(f"Rated {args.rating}/5: {url}")
    return 0


def cmd_request(delivery_conn, fb_conn, args):
    """Record a change request (add / change / remove a topic, feed, etc.)."""
    now = datetime.now(timezone.utc).isoformat()
    add_request(fb_conn, args.text, args.kind, now)
    print(f"Recorded request ({args.kind}): {args.text}")
    return 0


def cmd_list(delivery_conn, fb_conn, args):
    """Show recent feedback and open requests."""
    print("Recent feedback:")
    for row in get_feedback(fb_conn, limit=20):
        print(f"  {row['date']}  {row['rating']}/5  {row['url'][:70]}"
              + (f"  — {row['note']}" if row.get("note") else ""))
    print("\nOpen requests:")
    for row in get_open_requests(fb_conn):
        print(f"  [{row['kind']}] {row['text']}")
    return 0


def cmd_packet(delivery_conn, fb_conn, args):
    """Export a review packet for the AI-refinement loop.

    Each rated story is joined with the evidence behind its score —
    keywords, matched companies, section, source, publication date,
    and whether it was shown, skipped, or is still new — so a reviewer
    can tell WHY it scored as it did instead of guessing from the
    rating alone.
    """
    import state

    def load(name):
        with open(os.path.join(ROOT, "config", name), encoding="utf-8") as f:
            return json.load(f)

    rated_stories = []
    for fb in get_feedback(fb_conn, limit=50):
        story = {"rating": fb["rating"], "note": fb.get("note", ""),
                 "rated_on": fb["date"], "url": fb["url"]}
        cand = state.get_candidate_by_url(delivery_conn, fb["url"])
        if cand:
            story.update({
                "title": cand.get("title", ""),
                "score": cand.get("score", 0),
                "keywords": cand.get("keywords", []),
                "matched_companies": cand.get("matched_companies", []),
                "section": cand.get("section", ""),
                "source": cand.get("source", ""),
                "published": cand.get("published", ""),
                "outcome": cand.get("status", ""),
                "edition_date": cand.get("edition_date", ""),
            })
        rated_stories.append(story)

    packet = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "purpose": (
            "Review packet for tuning the AUMA daily briefing. Propose changes "
            "to keywords, weights, boosts, exclusions, feeds, or searches. "
            "Only propose changes to keys that already exist in the config "
            "files. Do not invent feed URLs. Do not change company lists "
            "unless a request below asks for it. Each rated story carries "
            "the score, keywords, matched companies, section, and outcome "
            "behind it — use that evidence, not the rating alone, to judge "
            "whether a keyword, a company match, or a boost caused it."
        ),
        "config": {
            "interests": load("interests.json"),
            "feeds": load("feeds.json"),
            "searches": load("searches.json"),
            "competitors": load("competitors.json"),
            "customers_count": len(load("customers.json")["customers"]),
        },
        "feedback": get_feedback(fb_conn, limit=50),
        "rated_stories": rated_stories,
        "open_requests": get_open_requests(fb_conn),
        "recent_editions": delivery_conn.execute(
            "SELECT date, item_count, status FROM editions "
            "ORDER BY date DESC LIMIT 14"
        ).fetchall(),
    }
    packet["recent_editions"] = [dict(r) for r in packet["recent_editions"]]
    with open(PACKET_PATH, "w", encoding="utf-8") as f:
        json.dump(packet, f, indent=2)
    print(f"Wrote {PACKET_PATH}")
    print("Give this file to an AI assistant and ask it to propose config changes.")
    print("Review every proposal yourself before applying it to config/*.json.")
    return 0


def main(argv=None):
    import state

    parser = argparse.ArgumentParser(description="AUMA daily briefing feedback")
    sub = parser.add_subparsers(dest="command", required=True)

    p_rate = sub.add_parser("rate", help="Rate a story 1-5")
    p_rate.add_argument("target", help="Story URL or candidate id number")
    p_rate.add_argument("rating", type=int, choices=[1, 2, 3, 4, 5])
    p_rate.add_argument("--note", default="")

    p_req = sub.add_parser("request", help="Request a change")
    p_req.add_argument("text", help='e.g. "add coverage of data center cooling"')
    p_req.add_argument("--kind", default="add",
                       choices=["add", "change", "remove", "other"])

    sub.add_parser("list", help="Show recent feedback and open requests")
    sub.add_parser("packet", help="Export a review packet for AI refinement")

    args = parser.parse_args(argv)
    delivery_conn = state.connect(DB_PATH)
    fb_conn = connect_feedback()
    # No legacy-row copy step: before round 2, feedback.py wrote into
    # state/briefing.db, but that storage was verified empty (0 rows)
    # on Oct 27, 2026, and state._migrate drops the old tables. If
    # feedback rows ever appear in briefing.db again, that is a bug —
    # this module is the only feedback writer.
    if args.command == "rate":
        return cmd_rate(delivery_conn, fb_conn, args)
    if args.command == "request":
        return cmd_request(delivery_conn, fb_conn, args)
    if args.command == "list":
        return cmd_list(delivery_conn, fb_conn, args)
    if args.command == "packet":
        return cmd_packet(delivery_conn, fb_conn, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
