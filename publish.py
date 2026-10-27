"""Publish a dated edition of the AUMA daily briefing.

Renders a static HTML page from templates/edition.html and writes it to
docs/<date>/index.html, plus docs/index.html as the latest edition (with
an archive list of past editions). GitHub Pages serves docs/ on the main
branch, so publishing is just writing files.

Rules from the design:
  - A dated receipt in the database makes rerunning a completed day a
    no-op (checked by run.py before collecting).
  - An unusable collection (zero picked items) leaves the previous public
    edition in place and records a 'failed' edition — it never publishes
    an empty page.
  - Partial source failures are shown in the edition, not hidden.
  - Items are marked 'shown' in the database only after the files are
    written, so a failed publish never burns the stories.
  - Files are written atomically (temp file + rename) so a failure can
    never leave a half-written page, and the shown-marking and the
    edition receipt commit together, so the database cannot say an
    edition exists that was never recorded (or vice versa).
  - Publication is staged, then promoted (round 2, N1): every page is
    rendered and written to a staging directory first. Only when all
    pages are staged AND the database transaction commits do the files
    move into docs/. A failure anywhere before promotion leaves docs/
    untouched — and the workflow only commits docs/ on a successful
    run, so a failed run can never publish partial output.
  - The edition's story order is recorded as a manifest (round 2, N2):
    section order, then pick order within each section. A --force
    rebuild renders from that manifest, so it reproduces the recorded
    edition instead of re-sorting it.
  - Links are relative to each page's own location: the dated page sits
    one directory down from the site root, the latest-edition copy sits
    at the root, and each gets the prefix its location needs.
  - A historical rebuild (an edition that is not the latest) updates
    only its dated page. The root page always shows the latest edition.
    (Round 2, N2: rebuilding an older date used to replace the homepage
    with that older edition.)

Run directly to test rendering on whatever is picked in the database:
    python3 publish.py
"""

import html
import os
import shutil
from datetime import datetime, timezone
from string import Template

ROOT = os.path.dirname(os.path.abspath(__file__))

SECTION_TITLES = {
    "industry": "Industry News",
    "customers": "Customers & Projects",
    "competitors": "Competitors",
    "wildcard": "Wildcard — Outside Your Usual Interests",
}
SECTION_ORDER = ["industry", "customers", "competitors", "wildcard"]


def esc(text):
    return html.escape(text or "")


def excerpt(text, limit=280):
    """Short excerpt; cut at a word boundary and add an ellipsis."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "…"


def format_published(iso_value):
    if not iso_value:
        return ""
    try:
        dt = datetime.fromisoformat(iso_value)
        return dt.strftime("%b %d, %Y")
    except ValueError:
        return ""


def render_item(item):
    tags = ""
    companies = item.get("matched_companies") or []
    if companies:
        tags = (
            '<div class="tags">'
            + "".join(f"<span>{esc(c)}</span>" for c in companies[:4])
            + "</div>"
        )
    return (
        '<article class="item">'
        f'<h3><a href="{esc(item["url"])}">{esc(item["title"])}</a></h3>'
        f'<p class="meta">{esc(item.get("source", ""))}'
        + (f" · {format_published(item.get('published', ''))}" if item.get("published") else "")
        + "</p>"
        + (f'<p class="summary">{esc(excerpt(item.get("summary", "")))}</p>' if item.get("summary") else "")
        + tags
        + "</article>"
    )


def render_sections(sections):
    parts = []
    for section in SECTION_ORDER:
        items = sections.get(section, [])
        if not items:
            continue
        parts.append(f'<section><h2>{SECTION_TITLES[section]}</h2>')
        parts.append("".join(render_item(item) for item in items))
        parts.append("</section>")
    return "\n".join(parts)


def render_failures(failures):
    """Human-readable note for sources that failed to load today."""
    if not failures:
        return ""
    names = ", ".join(f"{f['source']} ({esc(f.get('error', 'failed')[:80])})" for f in failures)
    return (
        "<p class=\"failures\"><strong>Note:</strong> some sources did not load "
        f"today: {names}. Items below come from the sources that worked.</p>"
    )


def _rel(prefix, target):
    """A site-relative link from a page whose directory is `prefix` deep.

    The dated page lives in <date>/ (prefix '..'), the latest-edition
    copy lives at the root (prefix ''). A leading '/' would mean the
    domain root, which is wrong for a project Pages site, so the prefix
    is joined only when it is non-empty.
    """
    return f"{prefix}/{target}" if prefix else target


def _atomic_write(path, content):
    """Write content to path via a temp file + rename: never half-written."""
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _atomic_copy(src, dst):
    """Copy a file via a temp file + rename: never half-written."""
    tmp = f"{dst}.tmp-{os.getpid()}"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def render_archive(archive_dates, current_date, prefix=""):
    if not archive_dates:
        return ""
    links = "".join(
        f'<li><a href="{_rel(prefix, d + "/index.html")}">{d}</a></li>'
        for d in archive_dates
        if d != current_date
    )
    if not links:
        return ""
    return f'<nav class="archive"><h2>Past editions</h2><ul>{links}</ul></nav>'


def _render_page(template, run_date, total, sections, failures, archive_dates,
                 generated_at, prefix):
    """One page's HTML, with links relative to that page's location."""
    return template.substitute(
        date=run_date,
        item_count=total,
        sections_html=render_sections(sections),
        failures_html=render_failures(failures),
        archive_html=render_archive(archive_dates, run_date, prefix),
        generated_at=generated_at,
        css_href=_rel(prefix, "style.css"),
    )


def publish(conn, sections, stats, failures, run_date, output_dir,
            run_id="", write_root=True, generated_at=None, created_at=None):
    """Write the edition. Returns ('published'|'failed', item_count, path).

    On 'failed' nothing is written and the previous edition stays live.

    Staging (round 2, N1): the dated page, the root page and the
    stylesheet are all rendered and written to a staging directory
    inside output_dir first. Only after every staged file exists are
    they promoted into place with os.replace — and only then does the
    database transaction commit. A failure during rendering or staging
    leaves docs/ exactly as it was; a failure during promotion is
    confined to os.replace of whole files (no half-written pages).
    The residual window — promotion succeeding but the DB commit
    failing — leaves new files with no receipt; a rerun then
    re-publishes (the stories are still 'new'), it never silently
    claims the edition exists. The workflow is the other half of
    this fix: it commits docs/ only on success.

    `write_root` is False for a historical rebuild: only the dated
    page is updated, the latest-edition copy at the root is left
    alone. (Round 2, N2.)

    `generated_at` overrides the "Generated ..." stamp. A rebuild
    passes the original edition's timestamp so the re-rendered page
    is identical to the original. (Round 2, N2.) `created_at` likewise
    overrides the receipt timestamp a rebuild records, so the edition
    receipt itself does not change on a rebuild.
    """
    import state  # local import so `python3 publish.py` works from repo root

    total = sum(len(v) for v in sections.values())
    if total == 0:
        state.record_edition(
            conn, run_date, "", 0,
            datetime.now(timezone.utc).isoformat(), "failed", run_id=run_id,
        )
        return "failed", 0, ""

    with open(os.path.join(ROOT, "templates", "edition.html"), encoding="utf-8") as f:
        template = Template(f.read())

    archive_dates = state.get_past_edition_dates(conn)
    if generated_at is None:
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    dated_html = _render_page(template, run_date, total, sections, failures,
                              archive_dates, generated_at, prefix="..")
    root_html = _render_page(template, run_date, total, sections, failures,
                             archive_dates, generated_at, prefix="")

    # Render order for the manifest: section order, then pick order
    # within each section — the exact order the pages display.
    ordered = [(item["id"], section)
               for section in SECTION_ORDER
               for item in sections.get(section, [])]

    staging = os.path.join(output_dir, f".staging-{run_id or os.getpid()}")
    os.makedirs(staging, exist_ok=True)
    try:
        _atomic_write(os.path.join(staging, "dated.html"), dated_html)
        if write_root:
            _atomic_write(os.path.join(staging, "root.html"), root_html)
            _atomic_copy(os.path.join(ROOT, "templates", "style.css"),
                         os.path.join(staging, "style.css"))

        edition_dir = os.path.join(output_dir, run_date)
        os.makedirs(edition_dir, exist_ok=True)
        os.replace(os.path.join(staging, "dated.html"),
                   os.path.join(edition_dir, "index.html"))
        if write_root:
            os.replace(os.path.join(staging, "root.html"),
                       os.path.join(output_dir, "index.html"))
            os.replace(os.path.join(staging, "style.css"),
                       os.path.join(output_dir, "style.css"))
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    # Only now are the stories marked shown, the manifest recorded and
    # the edition receipt written — in a single transaction, so a failed
    # write never burns them and the receipt can never go missing while
    # the stories are marked.
    ids = [item["id"] for items in sections.values() for item in items]
    state.record_published_edition(
        conn, ids, run_date, f"{run_date}/index.html", total,
        created_at or datetime.now(timezone.utc).isoformat(),
        run_id=run_id, ordered=ordered,
    )
    return "published", total, os.path.join(edition_dir, "index.html")


if __name__ == "__main__":
    import state

    from datetime import datetime

    conn = state.connect(os.path.join(ROOT, "state", "briefing.db"))
    import rank

    picked, stats = rank.rank(conn, os.path.join(ROOT, "config"), datetime.now(timezone.utc))
    failures = [
        {"source": r["source"], "error": r["error"]}
        for r in state.get_source_runs(conn, datetime.now(timezone.utc).date().isoformat())
        if not r["ok"]
    ]
    print(publish(conn, picked, stats, failures,
                  datetime.now(timezone.utc).date().isoformat(),
                  os.path.join(ROOT, "docs")))
