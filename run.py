"""Daily entry point for the AUMA daily briefing.

Runs the whole pipeline: collect -> rank -> publish.

    python3 run.py                 # today's edition (America/Chicago date)
    python3 run.py --date 2026-10-27
    python3 run.py --force         # rebuild today's edition from its recorded stories
    python3 run.py --db /tmp/test.db --output /tmp/site   # local test run

Rules:
  - If today's edition is already published (and --force is not given),
    the run is a no-op and exits 0. Rerunning a completed day changes
    nothing.
  - If collection fails for every source, no new edition is published:
    the previous public edition stays in place and the run exits 1.
    (Previously, unshown stories collected on earlier days could still
    produce a fresh-looking edition on a total-outage day.)
  - --force rebuilds the recorded edition from its own stories. It does
    not re-collect or re-rank, so the published edition cannot silently
    change under a rerun. The rebuild renders from the edition manifest
    (story ids in recorded render order), reuses the original run id,
    timestamp and failure note, and never replaces the homepage unless
    the rebuilt date IS the latest edition. (Round 2, N2.)
  - If ranking picks zero items, the previous public edition stays in
    place and the run exits 1.
  - The daily run uses zero LLM tokens and no paid APIs.
"""

import argparse
import os
import sys
import uuid
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
                        help="Rebuild this date's edition from its recorded stories")
    args = parser.parse_args(argv)

    run_date = args.date or chicago_today()
    run_id = f"{run_date}-{uuid.uuid4().hex[:8]}"
    conn = state.connect(args.db)

    # Dated receipt: rerunning a completed day is a no-op.
    existing = state.get_edition(conn, run_date)
    if existing and existing["status"] == "published" and not args.force:
        print(f"Edition {run_date} already published — nothing to do.")
        return 0

    # --force rebuilds the recorded edition from its own stories. No
    # collection, no re-ranking: the published edition is reproduced,
    # not replaced by a new selection. The rebuild reuses the ORIGINAL
    # run id, created_at and failure note, and renders from the edition
    # manifest (recorded render order) — so the rebuilt page is
    # identical to the original. A historical rebuild updates only its
    # dated page: the root page (the latest edition) is left alone
    # unless the rebuilt date is the latest published edition.
    # (Round 2, N2.)
    if existing and existing["status"] == "published" and args.force:
        items = state.get_edition_candidates(conn, run_date)
        if items:
            sections = {}
            for item in items:
                sections.setdefault(item.get("section", "industry"), []).append(item)
            failures = [
                {"source": r["source"], "error": r["error"]}
                for r in state.get_source_runs(
                    conn, run_date, run_id=existing.get("run_id") or None)
                if not r["ok"]
            ]
            stats = {"wildcard": len(sections.get("wildcard", []))}
            latest = state.get_past_edition_dates(conn, limit=1)
            write_root = bool(latest) and latest[0] == run_date
            created_at = existing.get("created_at") or ""
            generated_at = None
            if created_at:
                try:
                    generated_at = datetime.fromisoformat(created_at).strftime(
                        "%Y-%m-%d %H:%M UTC")
                except ValueError:
                    generated_at = None
            print(f"Rebuilding edition {run_date} from {len(items)} recorded stories"
                  + ("" if write_root else " (dated page only — not the latest edition)"))
            status, count, path = publish.publish(
                conn, sections, stats, failures, run_date, args.output,
                run_id=existing.get("run_id") or run_id,
                write_root=write_root, generated_at=generated_at,
                created_at=created_at or None,
            )
            if status == "failed":
                print("Rebuild FAILED: recorded edition is empty.")
                return 1
            print(f"Rebuilt {count} items -> {path}")
            return 0
        print(f"Edition {run_date} is recorded but has no stories — running normally.")

    print(f"=== AUMA daily briefing — {run_date} ===")
    now = datetime.now(timezone.utc)

    summary = collect.collect(conn, args.config, args.cache, run_date, run_id)
    print(
        f"Collect: {summary['feeds_ok']} feeds ok, {summary['feeds_failed']} failed; "
        f"{summary['searches_ok']} searches ok, {summary['searches_failed']} failed; "
        f"{summary['feeds_usable']} feeds usable, {summary['searches_usable']} searches usable; "
        f"{summary['new_candidates']} new candidates."
    )
    if summary["feeds_failed"] or summary["searches_failed"]:
        for failure in summary["failures"]:
            print(f"  FAILED: {failure['source']} — {failure['error']}")

    # Total outage: no source returned usable content. A source counts
    # only if it yielded at least one candidate — an HTTP 200 with an
    # empty feed is transport success, not evidence of fresh news.
    # Publishing from stories collected on earlier days would present
    # a stale selection as a fresh edition, so the previous edition
    # stays live instead. (Round 2, N3.)
    if summary["feeds_usable"] == 0 and summary["searches_usable"] == 0:
        state.record_edition(
            conn, run_date, "", 0,
            datetime.now(timezone.utc).isoformat(), "failed", run_id=run_id,
        )
        print("Publish FAILED: no source returned usable stories — previous edition left in place.")
        return 1

    picked, stats = rank.rank(conn, args.config, now, edition_date=run_date)
    total = sum(len(v) for v in picked.values())
    print(
        f"Rank: scored {stats['scored']}, picked {total} "
        f"({', '.join(f'{k}={len(v)}' for k, v in picked.items() if v)}), "
        f"wildcard {stats['wildcard']}."
    )

    failures = [
        {"source": r["source"], "error": r["error"]}
        for r in state.get_source_runs(conn, run_date, run_id=run_id)
        if not r["ok"]
    ]
    status, count, path = publish.publish(
        conn, picked, stats, failures, run_date, args.output, run_id=run_id
    )
    if status == "failed":
        print("Publish FAILED: no items picked — previous edition left in place.")
        return 1

    print(f"Published {count} items -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
