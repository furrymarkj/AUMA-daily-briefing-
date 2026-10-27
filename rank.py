"""Filter and rank candidates for the AUMA daily briefing.

Scoring (all rules live in config/interests.json — tune there, not here):
  - Keyword match in the headline: topic weight x 2. In the summary: x 1.
    Both use whole-word matching, so a short keyword cannot match inside
    an unrelated longer word in a summary.
  - Competitor company match: +12, section becomes 'competitors'.
  - Customer/project company match: +10, section becomes 'customers'.
  - Recency: published in the last 24h +20, 48h +10, 7 days +5.
  - Publisher-feed items get their feed's trust bonus.
  - Exclusion phrases drop an item outright.

Then: drop anything below min_score or older than max_age_days, drop
anything whose normalized title already appeared in a recent edition,
dedupe by title within the run, and apply per-source and per-section
caps so one publisher or subject cannot dominate. A small wildcard
section keeps recent feed items whose interest score (before the recency
and trust boosts) is below the threshold — discoveries outside the
usual interests. Wildcards obey the same total and per-source caps.

Company matching: distinctive names match case-insensitively. Short or
common-word names (Total, Shell, Dow, Beck, ...) match case-sensitively
and only with supporting context near the name — a company suffix after
any occurrence, or a domain cue word within 12 words of an occurrence —
so "Dow Jones" or "Beck resigns" does not tag a company. "EAST Texas"
as a place name is rejected unless company evidence is present. When
several records share one alias, the story is attributed to the record
that owns the alias (the parent company), not to every facility that
lists it.

Run directly to test ranking on whatever is in the database:
    python3 rank.py
"""

import json
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone

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

# A company suffix near an ambiguous name, or one of these domain
# words anywhere in the text, counts as supporting context for the
# match. The cues are deliberately industrial: ordinary municipal words
# such as "water" or "plant" appear in stories about towns, not companies.
COMPANY_SUFFIXES = {
    "inc", "corp", "corporation", "llc", "ltd", "co", "company", "group",
    "gmbh", "plc", "sa",
}
DOMAIN_CUES = {
    "valve", "actuator", "automation", "chemical", "refinery",
    "petrochemical", "pipeline", "lng", "epc", "offshore", "upstream",
    "midstream", "downstream",
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


def company_match(text, name):
    """True if a company name matches, with context for ambiguous names.

    Distinctive names match on the word alone. A short or common-word
    name (in CASE_SENSITIVE_NAMES) also needs a company suffix near the
    name ("Shell Oil Company", "Beck Inc.") or a domain cue word near
    the name ("Beck Automation"), so an unrelated use of the same word
    does not tag the company.

    (Round 2, N5: the cue used to be valid anywhere in the whole
    article — a refinery mentioned in an unrelated paragraph could
    validate an ambiguous name in the headline. Now the evidence must
    sit near the matched name itself: a suffix among the next 3 words
    after ANY occurrence, or a domain cue within 12 words either side
    of an occurrence. The EAST geographic guard below rejects "EAST
    Texas" as a place name unless company evidence is present.)
    """
    if not word_match(text, name):
        return False
    if name.lower() not in CASE_SENSITIVE_NAMES:
        return True
    pattern = r"\b" + re.escape(name) + r"\b"
    # Split the text into words once, with the word index of each match.
    words = [(m.group(0), m.start()) for m in re.finditer(r"\S+", text)]
    lowered_words = [w.lower().rstrip(".") for w, _ in words]
    for m in re.finditer(pattern, text):
        # Word index of this occurrence.
        idx = next(i for i, (_, pos) in enumerate(words) if pos >= m.start())
        # Suffix: a company suffix among the next 3 words after the name.
        after = lowered_words[idx + 1:idx + 4]
        if any(w in COMPANY_SUFFIXES for w in after):
            return True
        # Geographic guard for EAST: "EAST Texas" is a place, not the
        # company, unless a suffix follows or the full company name
        # ("Electric Actuator Service") appears in the text.
        if name == "EAST" and after and after[0] == "texas":
            if "electric actuator service" not in text.lower():
                continue
        # Domain cue: within 12 words either side of this occurrence.
        window = lowered_words[max(0, idx - 12):idx + 13]
        if any(w in DOMAIN_CUES for w in window):
            return True
    return False


def match_companies(text, companies, competitors):
    """Return (section, [matched company names]) for a headline+summary.

    Competitors win over customers if both match — a story about Rotork
    is competitor intel even if it also names a customer.

    For customers, several records can share one alias (a parent company
    and its facilities). Each alias resolves to one record: the record
    whose name IS the alias (the parent, which owns it), or, when only
    one record lists the alias, that record. When several facilities
    share an alias and none owns it, the alias itself is the tag — the
    company, with no facility implied. A more specific tag supersedes
    a generic one it contains.
    """
    matched_competitors = []
    for comp in competitors:
        names = [comp["name"]] + comp.get("variants", [])
        if any(company_match(text, n) for n in names):
            matched_competitors.append(comp["name"])
    if matched_competitors:
        return "competitors", matched_competitors

    alias_to_records = {}
    for cust in companies:
        names = [cust["name"]] + cust.get("variants", [])
        for n in names:
            if company_match(text, n):
                alias_to_records.setdefault(n, []).append(cust["name"])
    if not alias_to_records:
        return "industry", []

    resolved = []
    for alias, recs in alias_to_records.items():
        owners = [r for r in recs if r == alias]
        if owners:
            resolved.append(owners[0])
        elif len(recs) == 1:
            resolved.append(recs[0])
        else:
            resolved.append(alias)
    # Dedupe, and let a more specific tag supersede a generic one it contains.
    resolved = sorted(set(resolved), key=len)
    final = [t for t in resolved
             if not any(t != o and t in o for o in resolved)]
    return "customers", final


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
    """Score one candidate.

    Returns (score, base_score, keywords, companies, section). `score`
    includes the recency and trust boosts; `base_score` is the interest
    score before those boosts, and is what the wildcard pool is judged
    on — otherwise a recent feed item always clears the threshold on
    recency + trust alone and the pool can never fill.
    """
    title = cand.get("title", "")
    summary = cand.get("summary", "")
    text = f"{title} {summary}"

    # Exclusions drop the item outright.
    lowered = text.lower()
    for phrase in interests.get("exclusions", []):
        if phrase.lower() in lowered:
            return -1, -1, [], [], "industry"

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
            elif word_match(summary, kw):
                score += weight
                keywords.append(kw)

    section, companies = match_companies(text, customers, competitors)
    boosts = interests.get("boosts", {})
    if section == "competitors":
        score += boosts.get("competitor_match", 0)
    elif section == "customers":
        score += boosts.get("customer_match", 0)
    base_score = score

    # Recency. A future-dated story is treated as just published
    # (a timezone or feed-date skew), never as extra-fresh.
    published = parse_date(cand.get("published", ""))
    if published:
        age_hours = max(0.0, (now - published).total_seconds() / 3600)
        if age_hours <= 24:
            score += boosts.get("recency_hours_24", 0)
        elif age_hours <= 48:
            score += boosts.get("recency_hours_48", 0)
        elif age_hours <= 168:
            score += boosts.get("recency_hours_168", 0)

    # Publisher feeds are trusted trade press; searches are not.
    if cand.get("source_type") == "feed":
        score += cand.get("trust", 0)

    return score, base_score, sorted(set(keywords)), companies, section


def normalize_title(title):
    """Normalize for dedupe: lowercase, strip common prefixes/suffixes.

    Unicode-safe (round 2, N7): NFKD-normalize and fold accents first,
    so "café" and "cafe" share a fingerprint. A title that folds to
    nothing (entirely non-Latin script) falls back to its lowercased
    original with punctuation collapsed, so it still dedupes exactly
    instead of colliding on an empty key.
    """
    text = unicodedata.normalize("NFKD", title or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.encode("ascii", "ignore").decode("ascii").lower().strip()
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
    key = re.sub(r"[^a-z0-9]+", " ", text).strip()
    if key:
        return key
    raw = (title or "").lower().strip()
    return re.sub(r"\s+", " ", raw) if raw else ""


def rank(conn, config_dir, now, edition_date=None):
    """Score new candidates and pick the edition. Returns (sections, stats).

    sections: {'industry': [...], 'competitors': [...], 'customers': [...],
               'wildcard': [...]} with candidates sorted by score, then
    recency. stats: counts for the run summary.

    `edition_date` is the date of the edition being built (YYYY-MM-DD).
    The cross-day title-dedupe window is calculated from it when given,
    so a backdated --date run judges eligibility against its own date
    instead of today. (Round 2, N10.) When omitted it defaults to
    today's date, which is the normal daily-run behaviour.
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
    title_lookback_days = interests.get("title_lookback_days", 7)

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
    to_skip = []

    # Titles shown in recent editions: the same story must not be
    # published again under a different URL.
    ref_date = edition_date or now.date().isoformat()
    since = (datetime.fromisoformat(ref_date).date()
             - timedelta(days=title_lookback_days)).isoformat()
    recent_titles = state.get_recent_title_keys(conn, since)

    candidates = state.get_new_candidates(conn)
    scored = []
    wildcard_pool = []
    for cand in candidates:
        # The section the story was collected under (a search recipe's
        # hint, or a feed's default). It is the fallback when no company
        # matches; company identification wins when it does. Read from
        # source_section — the collection-time hint — not from section,
        # which ranking overwrites. (Round 2, N6.)
        orig_section = cand.get("source_section") or cand.get("section", "industry")
        # Publisher-feed trust bonus lives on the feed config, not the row.
        feed = feeds.get(cand.get("source", ""))
        cand["trust"] = feed.get("trust", 0) if feed else 0
        score, base_score, keywords, companies, section = score_candidate(
            cand, interests, customers, competitors, now
        )
        if section == "industry" and orig_section in ("customers", "competitors"):
            section = orig_section
        cand["score"] = score
        cand["keywords"] = keywords
        cand["matched_companies"] = companies
        cand["section"] = section
        # Persist the score/matches so the database reflects the ranking,
        # not just the collection-time defaults.
        state.update_scored(conn, cand)

        # Cross-day dedupe: this title already appeared recently.
        title_key = normalize_title(cand["title"])
        if title_key in recent_titles:
            stats["dropped_duplicate_title"] += 1
            to_skip.append(cand["id"])
            continue

        # Age for the max-age rule: publication date when known, else the
        # date the story was fetched. A future date counts as just
        # published; the comparison is a real duration, not whole days.
        published = parse_date(cand.get("published", ""))
        age_dt = published or parse_date(cand.get("fetched_at", ""))
        if age_dt:
            age = now - age_dt
            if age < timedelta(0):
                age = timedelta(0)
            if age > timedelta(days=max_age_days):
                stats["dropped_too_old"] += 1
                to_skip.append(cand["id"])
                continue
        if score < 0:
            stats["dropped_low_score"] += 1
            to_skip.append(cand["id"])
            continue
        stats["scored"] += 1
        # Wildcard pool: recent publisher-feed items whose interest
        # score (before recency/trust) is below the threshold. An item
        # can be in the pool and in the main-loop list at once: if the
        # main loop has room it is picked there, and if it loses on a
        # cap the wildcard loop gives it a second chance. The pool is
        # judged on the interest score, not the boosted score — otherwise
        # recency + trust always clear the threshold and the pool can
        # never fill.
        if cand.get("source_type") == "feed" and published:
            age_hours = max(0.0, (now - published).total_seconds() / 3600)
            if age_hours <= 48 and base_score < min_score:
                wildcard_pool.append(cand)
        if score < min_score:
            if cand["id"] not in {c["id"] for c in wildcard_pool}:
                stats["dropped_low_score"] += 1
                to_skip.append(cand["id"])
            continue
        scored.append(cand)

    # Highest score first; ties break toward the most recent story.
    scored.sort(key=lambda c: (c["score"], c.get("published", "")), reverse=True)

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

    # Wildcard: recent feed items outside the usual interests, up to the
    # wildcard cap — and inside the same total and per-source caps as the
    # rest of the edition. The whole pool is scanned, not just the first
    # few, so a duplicate at the top cannot starve the section.
    wildcard_pool.sort(key=lambda c: c.get("published", ""), reverse=True)
    # Candidates that lose ONLY on a capacity cap (wildcard/total/
    # per-source) stay 'new' for tomorrow — they were never rejected.
    # Only durable rejections (duplicate title here; age/exclusion/low
    # score earlier) are skipped. (Round 2, N4: the old code skipped
    # every unpicked pool candidate, including cap losers.)
    cap_losers = set()
    for cand in wildcard_pool:
        if len(picked["wildcard"]) >= caps.get("wildcard", 3):
            cap_losers.update(c["id"] for c in wildcard_pool
                              if c["id"] not in
                              {p["id"] for sec in picked.values() for p in sec})
            break
        if sum(len(v) for v in picked.values()) >= caps.get("total", 25):
            cap_losers.update(c["id"] for c in wildcard_pool
                              if c["id"] not in
                              {p["id"] for sec in picked.values() for p in sec})
            break
        if per_source.get(cand["source"], 0) >= caps.get("per_source", 5):
            cap_losers.add(cand["id"])
            continue
        title_key = normalize_title(cand["title"])
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        per_source[cand["source"]] = per_source.get(cand["source"], 0) + 1
        cand["section"] = "wildcard"
        picked["wildcard"].append(cand)
    stats["wildcard"] = len(picked["wildcard"])

    # Rejected candidates are marked skipped with the run's other writes,
    # so they are not rescored on every later day. Items that lost only
    # on a cap stay 'new' — they may fit tomorrow's edition. A pool item
    # that the main loop already picked is not rejected at all.
    picked_ids = {c["id"] for sec in picked.values() for c in sec}
    for cand in wildcard_pool:
        if cand["id"] not in picked_ids and cand["id"] not in cap_losers:
            to_skip.append(cand["id"])
    if to_skip:
        state.mark_skipped(conn, sorted(set(to_skip)))

    # Persist the scores. update_scored() does not commit by itself,
    # and leaving the transaction open blocks other connections to
    # this database (e.g. feedback.py) until the caller commits.
    conn.commit()

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
