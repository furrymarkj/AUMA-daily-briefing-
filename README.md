# AUMA Daily Briefing

A personal daily newspaper for Mark Johnson, Regional Sales Manager at AUMA
USA (electric actuators / valve automation), territory Texas / Louisiana /
Mississippi. Every morning it collects industry news, competitor intel, and
customer/project news, ranks it against his interests, and publishes a dated
static edition — like a newspaper, for his territory.

**The daily run uses zero LLM tokens and no paid APIs.** AI helps develop and
refine the system; the production pipeline is plain Python.

## How it works

1. **Collect** (`collect.py`) — Fetches publisher RSS/Atom feeds
   (`config/feeds.json`), a thinker/voice watchlist (`config/voices.json`),
   and Bing News RSS / Google News RSS searches (`config/searches.json`).
   Search queries rotate on a schedule instead of all running every day.
   Raw responses are cached under `cache/` so a collection can be inspected
   or replayed.
2. **Filter and rank** (`rank.py`) — Interests live in readable JSON:
   topics, keywords, weights, boosts, exclusions (`config/interests.json`).
   Python scores candidates by those rules plus recency, removes duplicates
   and anything already shown, and applies per-source and per-section caps
   so one publisher or subject does not dominate. A small wildcard section
   keeps recent discoveries outside the usual interests.
3. **Specialist sections** — Industry News, Customers & Projects,
   Competitors (DeZURIK, Beck, Rotork, Bettis/Emerson, Flowserve, Bernard,
   EIM, Henry Pratt, Scallon Controls — never treated as customers), and
   Wildcard. Customer matching uses Mark's real account lists
   (`config/customers.json`, seeded 2026-10-26 from his AUMA library
   watchlist: 169 accounts across 5 tiers). DXP Enterprises and DXP Water
   are separate companies; Ross Valve is its own entry.
4. **Publish** (`publish.py`) — Renders a static site into `docs/`:
   headlines, short attributed publisher excerpts, links to the originals.
   SQLite (`state/briefing.db`) tracks candidates, feedback, and delivery
   history. A dated receipt makes rerunning a completed day a no-op. An
   unusable collection leaves the previous public edition in place; partial
   source failures are shown in the edition.
5. **Feedback** (`feedback.py`) — Rate stories and request additions from
   the command line; `feedback.py packet` exports a review packet for the
   manual AI-refinement loop (below).

## Run it locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 run.py            # today's edition (America/Chicago date)
```

Output goes to `docs/<YYYY-MM-DD>/index.html`, and `docs/index.html` is
always the latest edition. Open `docs/index.html` in a browser to read it.

Useful options:

```bash
python3 run.py --date 2026-10-27     # a specific date
python3 run.py --force                # rerun even if today is published
python3 run.py --db /tmp/test.db --output /tmp/site   # test run, no side effects
python3 collect.py                    # collection only
python3 rank.py                       # ranking only (prints the picks)
python3 feedback.py rate <url-or-id> 4 --note "good, more of this"
python3 feedback.py request "add coverage of data center cooling"
python3 feedback.py packet            # export review_packet.json
```

## The daily run (GitHub Actions + GitHub Pages)

`.github/workflows/daily.yml` runs the pipeline every morning (~6:00 AM
Central — the cron is 11:00 UTC, which is 6:00 AM CDT / 5:00 AM CST) and
commits the edition (`docs/`) and the state database (`state/briefing.db`)
back to `main`, so delivery history persists between runs.

**Setup (one time, in the GitHub repo):**

1. Push this code to the repo.
2. Enable Pages: repo Settings → Pages → **Source: Deploy from a branch**,
   branch `main`, folder `/docs`. The site URL will be
   `https://furrymarkj.github.io/AUMA-daily-briefing-/`.
3. Confirm the first scheduled run appears in the Actions tab (or trigger
   it manually with "Run workflow").

> **Heads-up:** GitHub Pages on a free account only serves **public**
> repositories. This repo is currently private, so either make it public
> (the published site contains public news links only — no personal data,
> no credentials) or use a paid GitHub plan. Until Pages is enabled, the
> Action still runs and commits editions to `docs/`, but there is no live
> site URL.

## Tune your interests

Everything that matters is JSON — no code changes needed:

- `config/interests.json` — topics, keywords, weights, boosts, exclusions,
  score threshold, age limit, per-section caps.
- `config/feeds.json` — publisher RSS feeds. **Verify any new feed before
  adding it**: fetch the URL and confirm it returns HTTP 200 and parseable
  RSS/Atom with items. Dead feeds are logged and shown in the edition, not
  hidden.
- `config/searches.json` — Bing/Google News RSS queries, engines, rotation.
- `config/customers.json` / `config/competitors.json` — company names and
  variants to match on. Competitors are never treated as customers.
- `config/voices.json` — thinker/voice watchlist (empty for now; add only
  real people/publications with verified feed URLs).

## Feedback and AI refinement (manual loop)

1. Rate stories and file requests with `feedback.py`.
2. Run `python3 feedback.py packet` — it writes `review_packet.json` with
   the current config, your feedback, open requests, and recent edition
   stats.
3. Give the packet to an AI assistant and ask it to propose changes to
   keywords, weights, or search queries.
4. **Review every proposal yourself** before applying it to `config/*.json`.
   The app never retrains anything and never silently rewrites your
   interests because you clicked a story. Proposals must only touch keys
   that already exist in the config files, and must never invent feed URLs.

## VPS alternative (for later)

If you ever want off GitHub: the same `run.py` works on any small Linux
box. Serve `docs/` with Caddy (or any static server) and schedule it with
a systemd timer (`OnCalendar=*-*-* 06:00:00`, `America/Chicago` timezone).
Keep `state/briefing.db` on the same machine so delivery history persists.
No other changes are needed — the pipeline is already dependency-light and
has no server component.

## Limitations (honest ones)

- Keyword ranking misses nuance; a story can score high on a keyword and
  still be off-topic, and a relevant story can score low if it uses
  different words. Tune `config/interests.json` over time.
- Coverage depends on the configured feeds and searches. If a publisher
  has no working RSS feed, it only appears via search.
- Excerpts are the publishers' own summaries, not independently
  fact-checked analysis. Always read the original before acting on a story.
- Google News RSS links point at news.google.com redirect URLs, not the
  publisher directly (one extra hop for the reader).

## Files

| File | What it does |
|---|---|
| `run.py` | Daily entry point: collect → rank → publish |
| `collect.py` | Fetches feeds and searches, caches raw responses |
| `rank.py` | Scores, dedupes, caps, picks the edition |
| `publish.py` | Renders the static HTML edition |
| `state.py` | SQLite helpers (candidates, editions, feedback) |
| `feedback.py` | Rate stories, request changes, export review packet |
| `config/*.json` | Feeds, interests, searches, customers, competitors, voices |
| `templates/` | `edition.html` shell + `style.css` |
| `docs/` | Published editions (generated; `index.html` = latest) |
| `state/briefing.db` | Delivery history (committed on purpose) |
| `.github/workflows/daily.yml` | Scheduled daily run |
