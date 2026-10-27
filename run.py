"""Daily entry point for the AUMA daily briefing.

Runs the whole pipeline: collect -> rank -> publish.

    python3 run.py                 # today's edition (America/Chicago date)
    python3 run.py --date 2026-10-27
    python3 run.py --force         # rerun even if today is already published
    python3 run.py --db /tmp/test.db --output /tmp/site   # local test run

Rules:
  - If today's edition is already published (and --force is not given),
    the run is a no-op and exits 0. Rerunning a completed day changes
    nothing.
  - If collection fails for every source, or ranking picks zero items,
    the previous public edition stays in place and the run exits 1.
  - The daily run uses zero LLM tokens and no paid APIs.
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
CHICAGO = ZoneInfo("America/Chicago")


def chicago_today():
    return datetime.now(CHICAGO).date().isoformat()


def main(argv=None):
    import collect
    import publish
    import rank
    import state

    parser = argparse.ArgumentParser(description="AUMA daily briefing — daily run")
    parser.add_argument("--date", default=None, help="Edition date YYYY-MM-DD (default: today, Chicago time)")
    parser.add_argument("--db", default=os.path.join(ROOT, "state", "briefing.db"))
    parser.add_argument("--config", default=os.path.join(ROOT, "config"))
    parser.add_argument("--cache", default=os.path.join(ROOT, "cache"))
    parser.add_argument("--output", default=os.path.join(ROOT, "docs"))
    parser.add_argument("--force", action="store_true",
                        help="Rerun even if this date is already published")
    args = parser.parse_args(argv)

    run_date = args.date or chicago_today()
    conn = state.connect(args.db)

    # Dated receipt: rerunning a completed day is a no-op.
    existing = state.get_edition(conn, run_date)
    if existing and existing["status"] == "published" and not args.force:
        print(f"Edition {run_date} already published — nothing to do.")
        return 0

    print(f"=== AUMA daily briefing — {run_date} ===")
    now = datetime.now(timezone.utc)

    summary = collect.collect(conn, args.config, args.cache, run_date)
    print(
        f"Collect: {summary['feeds_ok']} feeds ok, {summary['feeds_failed']} failed; "
        f"{summary['searches_ok']} searches ok, {summary['searches_failed']} failed; "
        f"{summary['new_candidates']} new candidates."
    )
    if summary["feeds_failed"] or summary["searches_failed"]:
        for failure in summary["failures"]:
            print(f"  FAILED: {failure['source']} — {failure['error']}")

    picked, stats = rank.rank(conn, args.config, now)
    total = sum(len(v) for v in picked.values())
    print(
        f"Rank: scored {stats['scored']}, picked {total} "
        f"({', '.join(f'{k}={len(v)}' for k, v in picked.items() if v)}), "
        f"wildcard {stats['wildcard']}."
    )

    failures = [
        {"source": r["source"], "error": r["error"]}
        for r in state.get_source_runs(conn, run_date)
        if not r["ok"]
    ]
    status, count, path = publish.publish(
        conn, picked, stats, failures, run_date, args.output
    )
    if status == "failed":
        print("Publish FAILED: no items picked — previous edition left in place.")
        return 1

    print(f"Published {count} items -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
