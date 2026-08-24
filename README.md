# Newsdesk

A batch news agent for one reader. It sweeps ~56 sources across security,
AI/tech, markets and world news, checks what it finds, summarises it, and
writes one static page you open whenever you like.

Every card carries a **priority**, a **bottom line**, the **key facts**, a
full **summary**, **tags**, and **every source link**. Nothing reaches the page
without passing a validation gate.

Runs on free tiers only. Current cost: **zero**.

---

## Quick start

```bash
pip install -r requirements.txt
```

Put a free [Google AI Studio](https://aistudio.google.com) key in `.env`:

```bash
cp .env.example .env
```

```bash
python run.py --test-llm
```

```bash
python run.py
```

```bash
python -m http.server 8777 --directory site
```

Open <http://localhost:8777>.

## Commands

| Command | What it does |
|---|---|
| `python run.py` | Full batch run |
| `python run.py --dry-run` | Runs everything, writes nothing |
| `python run.py --no-social` | Skip GitHub and Bluesky collectors |
| `python run.py --no-extract` | Skip full-text fetch (fast, lower quality) |
| `python run.py --stop-after N` | Stop after stage N (1–7) |
| `python run.py --show` | Print the last published run |
| `python run.py --test-llm` | One tiny call to prove key and model |
| `python run.py --update-blocklist` | Refresh the credibility blocklist |
| `python -m pytest tests/ -q` | Run the tests |

## How it decides what to show you

**Priority** blends interest match (45%), independent corroboration (30%) and
source trust (25%). Security stories matching a `critical_signals` term —
"actively exploited", "added to the KEV" — are escalated regardless, and the
critical badge is hard-capped at five so it keeps meaning something.

**Verdict** answers a different question: how well established is this? It is
rule-based, not a score threshold, and deliberately separates *how many
outlets* from *how good the source is*.

| Badge | Meaning |
|---|---|
| `verified` | Primary artifact (CISA, CVE record, vendor advisory) or 3+ independent outlets |
| `corroborated` | Two independent outlets. Syndicated copies of one wire count as one |
| `established` | One outlet, but a known publication |
| `unverified` | One blog, repo or aggregator — one person's word |
| `disputed` | Sources disagree, or a second model could not confirm a claim |

Items are **flagged, not hidden**. Three things cause a hard drop and never
appear: a domain on the low-credibility blocklist, a publish date outside its
category's window, and a summary claim whose supporting quote is not actually
in the article. Every rejection is logged to `data/rejected.json`.

## Validation — two different questions

**Is the story real?** (stage 4, no model, free)
Independent outlet count with wire syndication collapsed, the Iffy.news
blocklist of ~2,000 low-credibility domains, a source tier list, and whether
the story traces to an official artifact.

**Is the summary faithful?** (stages 6 and 6b)
Stage 6 checks every claim's supporting quote actually appears in the source —
this catches a model inventing its own evidence, and works offline with no
key. Stage 6b asks a **different** model whether each claim really follows
from the source; a model grading its own work agrees with itself. It runs on
the cheapest high-quota tier, so the check is effectively free.

## Sources

| Kind | Count | Notes |
|---|---|---|
| Direct RSS | 43 | Every one probed live; dead feeds removed |
| Google News topic scans | 13 | Broad net per topic; redirect links resolved to real publishers |
| GitHub search | 11 queries | New and rising security/AI repos |
| Bluesky | 9 accounts | Individuals only — outlet accounts just repost their RSS |

**X/Twitter is not used.** Its free tier ended for new developers in February
2026 and reads are billed per post.

Windows are per category, because fields move at different speeds — a single
48h window starved every weekly-publishing security blog:

| Category | Window |
|---|---|
| markets, india_world | 48h |
| cyber_attacks | 96h |
| tech_ai | 120h |
| cyber_tools | 168h |

## Cost and quota

Free-tier limits differ enormously per model, so the pipeline walks a ladder
of eight, best-first, falling through on quota refusal, overload or malformed
output. Combined allowance is ~29,880 requests/day against the 2–7 a run uses.

Low-frequency material (social posts, repository listings) and minor items are
routed to the cheap high-quota models, keeping the flagship 20-a-day allowance
for stories at the top of the page.

A persistent per-model counter stops the run before it can overspend; a real
provider `429` is remembered for the rest of the UTC day. When everything is
exhausted the run still completes with extractive summaries — you always get a
page.

## Configuration

Everything behavioural lives in `config/`; you should not need to edit code.

| File | Purpose |
|---|---|
| `feeds.yaml` | Sources by category with tier / primary / aggregator flags |
| `interests.yaml` | Your profile, boost keywords, critical signals, mute list |
| `tags.yaml` | Controlled tag vocabulary and keyword rules |
| `settings.yaml` | Windows, clustering, trust, priority, quotas, model ladder |

## Layout

```
config/         feeds, interests, tags, settings
pipeline/       s1_harvest -> s7_publish, one module per stage
data/
  news.json     what the site reads
  rejected.json audit trail of everything the gate blocked
  cache/        seen, summaries, quota counter, resolved links, blocklist
  raw/          per-stage snapshots for debugging (gitignored)
site/           index.html + its copy of news.json
tests/          21 unit tests
```

Each stage takes a list and returns a list, snapshotting to `data/raw/`, so any
stage can be replayed or diffed on its own.

## Status

See [PLAN.md](PLAN.md) for the design, [PROGRESS.md](PROGRESS.md) for what was
built and what broke on the way, and [TASKS.md](TASKS.md) for the backlog.
