"""Filter and rank candidates for the AUMA daily briefing.

Scoring (all rules live in config/interests.json — tune there, not here):
  - Keyword match in the headline: topic weight x 2. In the summary: x 1.
  - Competitor company match: +12, section becomes 'competitors'.
  - Customer/project company match: +10, section becomes 'customers'.
  - Recency: published in the last 24h +20, 48h +10, 7 days +5.
  - Publisher-feed items get their feed's trust bonus.
  - Exclusion phrases drop an item outright.

Then: drop anything below min_score or older than max_age_days, drop
anything already shown in a past edition, dedupe by title, and apply
per-source and per-section caps so one publisher or subject cannot
dominate. A small wildcard section keeps recent items that scored below
the threshold but come from a publisher feed — discoveries outside the
usual interests.

Run directly to test ranking on whatever is in the database:
    python3 rank.py
"""

import json
import os
import re
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))

# Short or common-word company names are matched case-sensitively (with
# word boundaries) so "Total" the word does not match TotalEnergies, and
# "Shell" the word does not match Shell Oil. Distinctive names match
# case-insensitively.
CASE_SENSITIVE_NAMES = {
    "total", "shell", "dow", "beck", "pratt", "eim", "awc", "isv", "aiv",
    "bbp", "sit", "atwell", "alfa", "genesis", "bray", "ward", "apex",
    "east",  # "EAST" (Electric Actuator Service of Texas) must not match "East Texas"
}


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def word_match(text, name):
    """True if `name` appears in `text` as a whole word/phrase."""
    pattern = r"\b" + re.escape(name) + r"\b"
    if name.lower() in CASE_SENSITIVE_NAMES:
        return re.search(pattern, text) is not None
    return re.search(pattern, text, re.IGNORECASE) is not None


def match_companies(text, companies, competitors):
    """Return (section, [matched company names]) for a headline+summary.

    Competitors win over customers if both match — a story about Rotork
    is competitor intel even if it also names a customer.
    """
    matched_competitors = []
    for comp in competitors:
        names = [comp["name"]] + comp.get("variants", [])
        if any(word_match(text, n) for n in names):
            matched_competitors.append(comp["name"])
    if matched_competitors:
        return "competitors", matched_competitors

    matched_customers = []
    for cust in companies:
        names = [cust["name"]] + cust.get("variants", [])
        if any(word_match(text, n) for n in names):
            matched_customers.append(cust["name"])
    if matched_customers:
        return "customers", matched_customers
    return "industry", []


def parse_date(value):
    """Parse an ISO date-time; None when missing or unparsable."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def score_candidate(cand, interests, customers, competitors, now):
    """Score one candidate. Returns (score, keywords, companies, section)."""
    title = cand.get("title", "")
    summary = cand.get("summary", "")
    text = f"{title} {summary}"

    # Exclusions drop the item outright.
    lowered = text.lower()
    for phrase in interests.get("exclusions", []):
        if phrase.lower() in lowered:
            return -1, [], [], "industry"

    score = 0.0
    keywords = []
    for topic in interests.get("topics", []):
        weight = topic.get("weight", 0)
        for kw in topic.get("keywords", []):
            if not kw:
                continue
            if word_match(title, kw):
                score += weight * 2
                keywords.append(kw)
            elif kw.lower() in summary.lower():
                score += weight
                keywords.append(kw)

    section, companies = match_companies(text, customers, competitors)
    boosts = interests.get("boosts", {})
    if section == "competitors":
        score += boosts.get("competitor_match", 0)
    elif section == "customers":
        score += boosts.get("customer_match", 0)

    # Recency.
    published = parse_date(cand.get("published", ""))
    if published:
        age_hours = (now - published).total_seconds() / 3600
        if age_hours <= 24:
            score += boosts.get("recency_hours_24", 0)
        elif age_hours <= 48:
            score += boosts.get("recency_hours_48", 0)
        elif age_hours <= 168:
            score += boosts.get("recency_hours_168", 0)

    # Publisher feeds are trusted trade press; searches are not.
    if cand.get("source_type") == "feed":
        score += cand.get("trust", 0)

    return score, sorted(set(keywords)), companies, section


def normalize_title(title):
    """Normalize for dedupe: lowercase, strip common prefixes/suffixes."""
    text = title.lower().strip()
    for prefix in ("breaking news:", "breaking:", "product news:", "news:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    # Many feeds append " - Publisher" to the title; drop that tail so the
    # same story from two publishers still dedupes (only when the part
    # before the dash is already long and distinctive).
    if " - " in text:
        head = text.rsplit(" - ", 1)[0]
        if len(head) >= 30:
            text = head
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def rank(conn, config_dir, now):
    """Score new candidates and pick the edition. Returns (sections, stats).

    sections: {'industry': [...], 'competitors': [...], 'customers': [...],
               'wildcard': [...]} with candidates sorted by score, then
    recency. stats: counts for the run summary.
    """
    import state  # local import so `python3 rank.py` works from repo root

    interests = load_json(os.path.join(config_dir, "interests.json"))
    customers = load_json(os.path.join(config_dir, "customers.json"))["customers"]
    competitors = load_json(os.path.join(config_dir, "competitors.json"))["competitors"]
    feeds = {
        f["name"]: f
        for f in load_json(os.path.join(config_dir, "feeds.json"))["feeds"]
    }

    caps = interests.get("caps", {})
    min_score = interests.get("min_score", 5)
    max_age_days = interests.get("max_age_days", 14)

    stats = {
        "scored": 0,
        "dropped_low_score": 0,
        "dropped_too_old": 0,
        "dropped_duplicate_title": 0,
        "wildcard": 0,
    }

    picked = {"industry": [], "competitors": [], "customers": [], "wildcard": []}
    seen_titles = set()
    per_source = {}

    candidates = state.get_new_candidates(conn)
    scored = []
    wildcard_pool = []
    for cand in candidates:
        # Publisher-feed trust bonus lives on the feed config, not the row.
        feed = feeds.get(cand.get("source", ""))
        cand["trust"] = feed.get("trust", 0) if feed else 0
        score, keywords, companies, section = score_candidate(
            cand, interests, customers, competitors, now
        )
        cand["score"] = score
        cand["keywords"] = keywords
        cand["matched_companies"] = companies
        cand["section"] = section
        # Persist the score/matches so the database reflects the ranking,
        # not just the collection-time defaults.
        state.update_scored(conn, cand)

        published = parse_date(cand.get("published", ""))
        if published and (now - published).days > max_age_days:
            stats["dropped_too_old"] += 1
            continue
        if score < 0:
            stats["dropped_low_score"] += 1
            continue
        stats["scored"] += 1
        if score < min_score:
            # Wildcard pool: recent publisher-feed items outside the
            # usual interests. They get a second chance below.
            if cand.get("source_type") == "feed" and published:
                age_hours = (now - published).total_seconds() / 3600
                if age_hours <= 48:
                    wildcard_pool.append(cand)
            else:
                stats["dropped_low_score"] += 1
            continue
        scored.append(cand)

    # Highest score first; ties break toward the most recent story.
    scored.sort(key=lambda c: (-c["score"], c.get("published", "")), reverse=False)
    scored.sort(key=lambda c: c.get("published", ""), reverse=True)
    scored.sort(key=lambda c: -c["score"])

    for cand in scored:
        section = cand["section"]
        if len(picked[section]) >= caps.get(section, 99):
            continue
        if per_source.get(cand["source"], 0) >= caps.get("per_source", 5):
            continue
        title_key = normalize_title(cand["title"])
        if title_key in seen_titles:
            stats["dropped_duplicate_title"] += 1
            continue
        if sum(len(v) for v in picked.values()) >= caps.get("total", 25):
            break
        seen_titles.add(title_key)
        per_source[cand["source"]] = per_source.get(cand["source"], 0) + 1
        picked[section].append(cand)

    # Wildcard: up to N recent feed items that scored below the threshold.
    wildcard_pool.sort(key=lambda c: c.get("published", ""), reverse=True)
    for cand in wildcard_pool[: caps.get("wildcard", 3)]:
        title_key = normalize_title(cand["title"])
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        cand["section"] = "wildcard"
        picked["wildcard"].append(cand)
    stats["wildcard"] = len(picked["wildcard"])

    return picked, stats


if __name__ == "__main__":
    import state

    from datetime import datetime

    conn = state.connect(os.path.join(ROOT, "state", "briefing.db"))
    picked, stats = rank(conn, os.path.join(ROOT, "config"), datetime.now(timezone.utc))
    print("stats:", stats)
    for section, items in picked.items():
        print(f"\n== {section} ({len(items)}) ==")
        for item in items:
            print(f"  [{item['score']:>5.0f}] {item['title'][:90]} ({item['source']})")
