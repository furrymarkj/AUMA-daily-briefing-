"""Feedback and the AI-refinement loop for the AUMA daily briefing.

No website, no login, no authenticated endpoint in v1 — just a small
command-line tool that records ratings and change requests in the state
database:

    python3 feedback.py rate <story-url-or-id> <1-5> [--note "too much oil & gas"]
    python3 feedback.py request "add coverage of data center cooling"
    python3 feedback.py list
    python3 feedback.py packet

`packet` exports a review packet (review_packet.json) with the current
config, recent feedback, open requests, and recent edition stats. That
packet is the input for the manual AI-refinement loop: give it to an AI
assistant, which proposes changes to keywords, weights, or search
queries. Proposals are validated (the packet has a schema; the app
checks proposed JSON parses and only touches config keys it knows) and
are applied only when you explicitly accept them — nothing is ever
rewritten silently because you clicked a story.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "state", "briefing.db")
PACKET_PATH = os.path.join(ROOT, "review_packet.json")


def chicago_today():
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/Chicago")).date().isoformat()


def cmd_rate(conn, args):
    """Rate a story 1-5. Accepts a URL or a candidate id number."""
    import state

    target = args.target
    url = target
    if target.isdigit():
        row = conn.execute(
            "SELECT url FROM candidates WHERE id = ?", (int(target),)
        ).fetchone()
        if not row:
            print(f"No candidate with id {target}.")
            return 1
        url = row["url"]
    now = datetime.now(timezone.utc).isoformat()
    state.add_feedback(conn, chicago_today(), url, args.rating, args.note or "", now)
    print(f"Rated {args.rating}/5: {url}")
    return 0


def cmd_request(conn, args):
    """Record a change request (add / change / remove a topic, feed, etc.)."""
    import state

    now = datetime.now(timezone.utc).isoformat()
    state.add_request(conn, args.text, args.kind, now)
    print(f"Recorded request ({args.kind}): {args.text}")
    return 0


def cmd_list(conn, args):
    """Show recent feedback and open requests."""
    import state

    print("Recent feedback:")
    for row in state.get_feedback(conn, limit=20):
        print(f"  {row['date']}  {row['rating']}/5  {row['url'][:70]}"
              + (f"  — {row['note']}" if row.get("note") else ""))
    print("\nOpen requests:")
    for row in state.get_open_requests(conn):
        print(f"  [{row['kind']}] {row['text']}")
    return 0


def cmd_packet(conn, args):
    """Export a review packet for the AI-refinement loop."""
    import state

    def load(name):
        with open(os.path.join(ROOT, "config", name), encoding="utf-8") as f:
            return json.load(f)

    packet = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "purpose": (
            "Review packet for tuning the AUMA daily briefing. Propose changes "
            "to keywords, weights, boosts, exclusions, feeds, or searches. "
            "Only propose changes to keys that already exist in the config "
            "files. Do not invent feed URLs. Do not change company lists "
            "unless a request below asks for it."
        ),
        "config": {
            "interests": load("interests.json"),
            "feeds": load("feeds.json"),
            "searches": load("searches.json"),
            "competitors": load("competitors.json"),
            "customers_count": len(load("customers.json")["customers"]),
        },
        "feedback": state.get_feedback(conn, limit=50),
        "open_requests": state.get_open_requests(conn),
        "recent_editions": conn.execute(
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
    conn = state.connect(DB_PATH)
    if args.command == "rate":
        return cmd_rate(conn, args)
    if args.command == "request":
        return cmd_request(conn, args)
    if args.command == "list":
        return cmd_list(conn, args)
    if args.command == "packet":
        return cmd_packet(conn, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
