# Newsdesk — Plan

A batch news agent. It gathers the last 48 hours across four interest areas,
validates what it finds, summarises it, and publishes one static page you open
whenever you want. No push notifications, no inbox, no live server.

## Design decisions already settled

| Decision | Choice | Why |
|---|---|---|
| Delivery | A website you open | Not email/Telegram. Page load must be instant. |
| Cadence | One batch run, not continuous | Corroboration counts are only complete when the whole window is seen at once. Hourly slices mis-score a breaking story as single-source for its first hour. |
| Hosting | GitHub Actions does the work, Vercel serves static output | Vercel Hobby cron is **once per day, 10s timeout, UTC only** — it cannot run a 3-minute pipeline. Actions has no such limit. |
| State | JSON files in the repo | No database to run or pay for. Git history doubles as the digest archive. |
| LLM | Free tier (Gemini 2.5 Flash ~1500 req/day) | ~20 calls/day needed. Free is genuinely sufficient, not a compromise. |
| Cost | Zero | Actions free tier + Vercel Hobby + Gemini free tier. |

## The two validation questions

These are different problems and need different mechanisms. Conflating them
is the main way projects like this quietly go wrong.

**1. Is the story real?** — stage 4, mechanical, no model.
- independent outlet count (wire syndication collapsed to one vote)
- Iffy.news blocklist of ~2,000 low-credibility domains
- source tier list (A wire/official, B established, C aggregator)
- primary-source anchor (CISA KEV, CVE record, SEC/RBI filing, vendor advisory)

**2. Is my summary faithful?** — stage 6, the gate.
- forced citation at generation (every claim carries its source span)
- span check: is the quoted span actually in the article? (live)
- NLI entailment per claim (Step 4)
- LLM judge on borderline cases only (Step 4)

A contradicted claim is never published. Everything else is published **with a
badge**, because a dashboard that hides its uncertainty is less trustworthy
than one that shows it.

## Pipeline

```
1 harvest      33 feeds, parallel, 48h window
2 clean        blocklist, mute terms, dedupe, cluster same-story
3 extract      full article text (trafilatura), lead article per cluster
4 corroborate  outlet count, primary anchor -> trust score + priority
5 summarize    LLM -> summary, one-liner, importance, claims[]
6 verify       THE GATE: claim grounding; rejects never reach the site
7 publish      news.json + site/news.json, seen-store updated
```

Every stage takes a list and returns a list, and snapshots to `data/raw/`, so
any stage can be replayed or diffed on its own.

## Build steps

- [x] **Step 0** — architecture
- [x] **Step 1** — harvest + clean + cluster + trust scoring, console output
- [ ] **Step 2** — Gemini summarisation (`llm.provider: gemini`)
- [ ] **Step 3** — NLI entailment gate (`verify.enabled: true`)
- [ ] **Step 4** — GitHub Actions daily workflow
- [ ] **Step 5** — Vercel deploy

## Known limits of the current heuristic

Recorded honestly so Step 2 has a target to beat:

- **Relevance is keyword-based.** It cannot tell "RBI fines a lender" (news)
  from "RBI weekly treasury bill auction" (routine). Damped with a
  `routine_markers` list, which is a patch, not a fix. The LLM importance
  score replaces this.
- **Clustering is lexical.** Two outlets that paraphrase a headline with no
  shared content words will not cluster, so corroboration undercounts.
  Embeddings would fix it; not needed at this volume yet.
- **Extraction succeeds ~79% of the time** (357/449 on a live run, up from
  52% once per-category windows stopped starving the slower sources). The
  rest fall back to feed blurbs, which weakens summary and verification.
- ~~Google News links cannot be resolved~~ — **solved.** The first attempt
  only tried decoding the token offline, which fails for the current
  `AU_yq…` format. Google's own front end calls a `batchexecute` endpoint
  with a signature scraped from the article page; that returns the publisher
  URL. See `pipeline/gnews.py`. This also made Google News topic *search*
  usable, which is where most of the breadth now comes from.


## Vercel deployment — settings that matter

**Set the project root to `site/`, not the repository root.** The repo root
contains `data/rejected.json`, `data/cache/*` and the project docs. None hold
secrets — that was audited — but rejected items and internal caches are not
meant to be public, and serving the repo root publishes all of them.

If deploying with the `vercel` CLI from this directory rather than through the
Git integration, confirm `.env` is excluded from the upload set. The Git
integration never sees it (it is gitignored); a CLI deploy uploads the working
directory and can.
