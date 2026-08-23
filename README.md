# Newsdesk

A batch news agent. It sweeps the last 48 hours across cybersecurity, tech/AI,
markets and India/world news, checks what it finds, summarises it, and writes
one static page you open whenever you like.

Every card carries a **priority**, a **one-line "why this matters"**, a
**summary**, and **every source link** — plus a verdict badge saying how well
corroborated the story is.

Nothing reaches the page without passing a validation gate.

## Quick start

```bash
pip install -r requirements.txt
```

```bash
python run.py --update-blocklist
```

```bash
python run.py
```

Then serve the page:

```bash
python -m http.server 8777 --directory site
```

Open <http://localhost:8777>.

## Commands

| Command | What it does |
|---|---|
| `python run.py` | Full batch run, writes `data/news.json` and `site/news.json` |
| `python run.py --dry-run` | Runs everything, writes nothing |
| `python run.py --no-extract` | Skips full-text fetch — much faster, lower quality |
| `python run.py --stop-after 2` | Stops after stage N (1–7) |
| `python run.py --show` | Pretty-prints the last published run |
| `python run.py --update-blocklist` | Refreshes the Iffy.news credibility list |
| `python -m pytest tests/ -q` | Run the tests |

## How it decides what to show you

**Priority** blends three things: how much the story matches your interest
profile (45%), how many independent outlets carry it (30%), and how
trustworthy the sourcing is (25%). Security stories matching a
`critical_signals` term — "actively exploited", "added to the KEV" — are
escalated regardless of blend, and the critical badge is hard-capped at five
items so it keeps meaning something.

**Verdict** answers a separate question: how well established is this?

| Badge | Meaning |
|---|---|
| `verified` | Multiple independent outlets, or anchored to a primary source (CISA, CVE, SEC, RBI, vendor advisory) |
| `reported` | Several outlets, no primary anchor |
| `single source` | One outlet only — shown, but flagged |
| `disputed` | Outlets contradict each other on a key fact |

Items are **flagged, not hidden**. Only three things cause a hard drop and
never appear: a domain on the Iffy low-credibility blocklist, a publish date
outside the window (recycled content), and a summary claim whose supporting
quote is not actually in the article.

Rejections are logged to `data/rejected.json` so a bad call can be traced.

Every card has a **"why this scored the way it did"** toggle showing the full
audit trail.

## Configuration

Everything behavioural lives in `config/`; you should not need to edit code.

| File | Purpose |
|---|---|
| `feeds.yaml` | The 33 feeds, by category, with `tier` / `primary` / `aggregator` flags |
| `interests.yaml` | Your profile, per-category boost keywords, `critical_signals`, mute list |
| `settings.yaml` | Window, clustering thresholds, trust weights, priority buckets, quotas, LLM provider |

## Layout

```
config/         feeds, interests, settings
pipeline/       s1_harvest -> s7_publish, one module per stage
data/
  news.json     what the site reads
  rejected.json audit trail of everything the gate blocked
  cache/        seen-store + credibility blocklist
  raw/          per-stage snapshots for debugging (gitignored)
site/           index.html + its copy of news.json
tests/          unit tests for the pure logic
```

Each stage takes a list and returns a list, snapshotting to `data/raw/`, so any
stage can be replayed or diffed on its own.

## Status

Step 1 of 5 is done: harvest, cleaning, clustering, corroboration, trust
scoring and publishing all run against live data. Summarisation is currently
**extractive** (first sentences of the article) because no LLM is configured —
the page will say so. See [PLAN.md](PLAN.md) for the design and
[PROGRESS.md](PROGRESS.md) for what's built and what broke along the way.

To enable real summarisation, get a free Gemini API key, set
`llm.provider: gemini` in `config/settings.yaml`, and export `GEMINI_API_KEY`.
