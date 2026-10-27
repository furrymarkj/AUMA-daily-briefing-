"""Collect candidates for the AUMA daily briefing.

Sources (no API keys, no LLM, no paid services):
  1. Publisher RSS/Atom feeds listed in config/feeds.json.
  2. Bing News RSS and Google News RSS searches from config/searches.json.
     Search recipes rotate on a schedule (see searches.json) instead of
     all running every day.
  3. Thinker/voice watchlist feeds from config/voices.json (empty for now).

Raw responses are cached under cache/ (one file per source per day) so a
collection can be inspected or replayed without re-fetching. Every source
result — success or failure — is recorded in the state database, and the
edition shows partial failures rather than hiding them.

Run directly to test collection:
    python3 collect.py
"""

import hashlib
import html
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from urllib.parse import quote_plus, urlparse, urlunparse

import feedparser
import requests

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TIMEOUT = 20

# Repo root = the directory this file lives in.
ROOT = os.path.dirname(os.path.abspath(__file__))


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def normalize_url(url):
    """Strip tracking parameters and fragments so the same story dedupes."""
    parsed = urlparse(url.strip())
    query = "&".join(
        part
        for part in parsed.query.split("&")
        if part and not part.split("=")[0].lower().startswith(("utm_", "gclid", "fbclid"))
    )
    return urlunparse(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", query, "")
    )


def clean_text(text):
    """Strip HTML tags/entities and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_published(entry):
    """Best-effort ISO date-time for a feed entry; '' when unknown."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        try:
            return datetime(*parsed[:6], tzinfo=timezone.utc).isoformat()
        except (TypeError, ValueError):
            return ""
    return ""


def fetch(url):
    """Fetch a URL as text. Raises on HTTP errors and timeouts."""
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.text


def cache_raw(cache_dir, source_name, run_date, text):
    """Save a raw response for inspection/replay. Returns the file path."""
    os.makedirs(cache_dir, exist_ok=True)
    digest = hashlib.sha1(source_name.encode("utf-8")).hexdigest()[:10]
    path = os.path.join(cache_dir, f"{run_date}_{digest}.xml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def entries_to_candidates(parsed, source_name, source_type, section, fetched_at):
    """Turn parsed feed entries into candidate dicts."""
    out = []
    for entry in parsed.entries:
        link = entry.get("link", "").strip()
        title = clean_text(entry.get("title", ""))
        if not link or not title:
            continue
        summary = clean_text(
            entry.get("summary", "") or entry.get("description", "")
        )
        out.append(
            {
                "url": normalize_url(link),
                "title": title,
                "summary": summary[:600],
                "source": source_name,
                "source_type": source_type,
                "section": section,
                "published": parse_published(entry),
                "fetched_at": fetched_at,
            }
        )
    return out


def search_url(engine, query):
    """Build a no-key RSS search URL for an engine."""
    q = quote_plus(query)
    if engine == "google":
        return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    if engine == "bing":
        return f"https://www.bing.com/news/search?q={q}&format=rss"
    raise ValueError(f"Unknown search engine: {engine}")


def due_today(recipe, run_date):
    """True if a search recipe runs on this date.

    'daily' always runs. {'rotate': N, 'offset': k} runs when the number
    of days since 2026-01-01, mod N, equals k.
    """
    schedule = recipe.get("schedule", "daily")
    if schedule == "daily":
        return True
    if isinstance(schedule, dict) and "rotate" in schedule:
        epoch = date(2026, 1, 1)
        y, m, d = (int(p) for p in run_date.split("-"))
        days = (date(y, m, d) - epoch).days
        return days % schedule["rotate"] == schedule.get("offset", 0)
    return True


def collect(conn, config_dir, cache_dir, run_date):
    """Fetch all due sources, store candidates, record results.

    Returns a summary dict with per-kind counts and a failures list.
    A failure in one source never stops the others.
    """
    import state  # local import so `python3 collect.py` works from repo root

    fetched_at = datetime.now(timezone.utc).isoformat()
    summary = {
        "feeds_ok": 0,
        "feeds_failed": 0,
        "searches_ok": 0,
        "searches_failed": 0,
        "new_candidates": 0,
        "failures": [],
    }

    feeds = load_json(os.path.join(config_dir, "feeds.json"))["feeds"]
    for feed in feeds:
        if not feed.get("enabled", True):
            continue
        name = feed["name"]
        try:
            raw = fetch(feed["url"])
            cache_raw(cache_dir, name, run_date, raw)
            parsed = feedparser.parse(raw)
            if parsed.bozo and not parsed.entries:
                raise ValueError(f"Unparseable feed: {parsed.bozo_exception}")
            candidates = entries_to_candidates(
                parsed, name, "feed", feed.get("section", "industry"), fetched_at
            )
            before = conn.total_changes
            for cand in candidates:
                state.upsert_candidate(conn, cand)
            conn.commit()
            summary["new_candidates"] += conn.total_changes - before
            summary["feeds_ok"] += 1
            state.record_source_run(conn, run_date, name, "feed", len(candidates), True)
        except Exception as exc:  # one bad feed must not kill the run
            summary["feeds_failed"] += 1
            summary["failures"].append({"source": name, "error": str(exc)[:200]})
            state.record_source_run(conn, run_date, name, "feed", 0, False, str(exc)[:200])

    searches = load_json(os.path.join(config_dir, "searches.json"))["searches"]
    for recipe in searches:
        if not due_today(recipe, run_date):
            continue
        label = f"{recipe['engine']}: {recipe['query']}"
        try:
            raw = fetch(search_url(recipe["engine"], recipe["query"]))
            cache_raw(cache_dir, label, run_date, raw)
            parsed = feedparser.parse(raw)
            if parsed.bozo and not parsed.entries:
                raise ValueError(f"Unparseable feed: {parsed.bozo_exception}")
            candidates = entries_to_candidates(
                parsed, label, "search", recipe.get("section", "industry"), fetched_at
            )
            before = conn.total_changes
            for cand in candidates:
                state.upsert_candidate(conn, cand)
            conn.commit()
            summary["new_candidates"] += conn.total_changes - before
            summary["searches_ok"] += 1
            state.record_source_run(conn, run_date, label, "search", len(candidates), True)
        except Exception as exc:
            summary["searches_failed"] += 1
            summary["failures"].append({"source": label, "error": str(exc)[:200]})
            state.record_source_run(conn, run_date, label, "search", 0, False, str(exc)[:200])

    voices = load_json(os.path.join(config_dir, "voices.json"))["voices"]
    for voice in voices:
        if voice.get("type") == "search":
            label = f"voice search: {voice['name']}"
            try:
                raw = fetch(search_url("bing", voice["name"]))
                cache_raw(cache_dir, label, run_date, raw)
                parsed = feedparser.parse(raw)
                candidates = entries_to_candidates(
                    parsed, voice["name"], "search", "industry", fetched_at
                )
                for cand in candidates:
                    state.upsert_candidate(conn, cand)
                conn.commit()
                summary["searches_ok"] += 1
                state.record_source_run(conn, run_date, label, "search", len(candidates), True)
            except Exception as exc:
                summary["searches_failed"] += 1
                summary["failures"].append({"source": label, "error": str(exc)[:200]})
                state.record_source_run(conn, run_date, label, "search", 0, False, str(exc)[:200])
        else:
            name = voice["name"]
            try:
                raw = fetch(voice["feed_url"])
                cache_raw(cache_dir, name, run_date, raw)
                parsed = feedparser.parse(raw)
                candidates = entries_to_candidates(
                    parsed, name, "feed", "industry", fetched_at
                )
                for cand in candidates:
                    state.upsert_candidate(conn, cand)
                conn.commit()
                summary["feeds_ok"] += 1
                state.record_source_run(conn, run_date, name, "feed", len(candidates), True)
            except Exception as exc:
                summary["feeds_failed"] += 1
                summary["failures"].append({"source": name, "error": str(exc)[:200]})
                state.record_source_run(conn, run_date, name, "feed", 0, False, str(exc)[:200])

    return summary


if __name__ == "__main__":
    import state

    run_date = datetime.now(timezone.utc).date().isoformat()
    conn = state.connect(os.path.join(ROOT, "state", "briefing.db"))
    print(collect(conn, os.path.join(ROOT, "config"), os.path.join(ROOT, "cache"), run_date))
