# Newsdesk — Progress

Running log so any session (or any other tool) can resume cold.
Read [PLAN.md](PLAN.md) first for the design; this file is state only.

---

## 2026-08-23 — Step 1 complete, skeleton runs end to end

**Status:** stages 1–7 all execute. Stages 1, 2, 4, 7 are fully implemented.
Stages 3, 5, 6 run with honest fallbacks and raise `NotImplementedError`
rather than half-working if you switch them on before they are built.

### Live run numbers

| Metric | Value |
|---|---|
| Feeds configured | 33 (2 dead ones removed) |
| Articles harvested (48h) | ~400 |
| Dropped by Iffy blocklist | 44 |
| Clusters formed | ~315, of which ~11 multi-outlet |
| Full-text extraction success | 211/404 (~52%) |
| Published | 40, quota-balanced across 4 categories |
| Priority split | 1 critical / 55 important / 257 minor |

### Defects found by running it, and fixed

| # | Defect | Fix |
|---|---|---|
| 1 | 5 feeds died on `CERTIFICATE_VERIFY_FAILED` | Windows Python has no usable CA store; now uses `certifi`. Verification stays **on**. |
| 2 | PIB + TechCrunch returned 403 | Bot UA rejected; switched to a browser UA string. |
| 3 | **0 multi-outlet clusters** — corroboration, the core of the design, was dead | Jaccard threshold 0.72 was unreachable. Measured real same-story pairs at 0.33–0.60 Jaccard. Switched to the **overlap coefficient** (shared / shorter headline) at **0.55 + ≥3 shared tokens**: recovered every genuine pair, zero false positives. |
| 4 | **Every item scored `minor`** | Buckets were 75/50 but the blend topped out at 49 — thresholds set against an imagined 0–100 spread. Recalibrated to 58/38 against the observed range, plus a saturating interest curve. |
| 5 | **All 5 "critical" items were routine govt press releases** | `primary_bonus` was feeding the *interest* score. An official source makes a story **trustworthy, not important**. Bonus removed from interest entirely; kept in trust. Added `routine_markers` damping. |
| 6 | Reference links were opaque Google News tokens | New-style `AU_yq…` tokens need a server round-trip; not resolvable offline. Feeds now tagged `aggregator: true`, and a real outlet's link wins as lead. |
| 7 | HTML tags leaked into summaries | `strip_html()` applied at harvest. |
| 8 | Console crashed on box-drawing chars | Windows cp1252; stdout/stderr forced to UTF-8. |
| 9 | Category headings repeated 10× | Renderer grouped on *change* of category over priority-sorted input; now buckets first. |
| 10 | **39 of 40 cards were one category** | No balancing. Added `per_category_max: 12` with spare-slot refill. |
| 11 | **A second run in the same day showed only leftovers** | The seen-store was suppressing re-*display*. For a rolling-window dashboard it must only suppress re-*summarising*. Added `dedupe.mode: reuse_summary`. |
| 12 | Iffy blocklist URL 404'd | Real source is a CC-licensed Google Sheet; CSV export wired up. 2,005 domains cached. |

### Not yet done

- Stage 5 real summarisation — set `llm.provider: gemini` + `GEMINI_API_KEY`
- Stage 6 NLI entailment — set `verify.enabled: true`
- Summary caching by uid (needed before `reuse_summary` actually saves money)
- GitHub Actions workflow (`.github/workflows/` written, untested)
- Vercel deploy

### Next action

Get a free Gemini API key, then wire stage 5. Everything upstream is proven
against live data.

---

## 2026-08-23 (later) — Step 2 built, awaiting a key

**Status:** Gemini provider written and wired. `config/settings.yaml` now has
`llm.provider: gemini`. Verified against the live endpoint with a deliberately
invalid key: the request is well-formed, reaches Google, and the 400 is
correctly classified as a configuration error instead of being retried.

Untested against a real key at time of writing.

### What was added

- `pipeline/llm_gemini.py` — REST client, no SDK, so the dependency list is
  unchanged. Structured output via `responseSchema`, so the model cannot
  return prose and stage 6 always has claims to verify.
- Summary cache (`data/cache/summaries.json`), keyed by cluster + body length.
  The dashboard shows a rolling window, so the same article appears in several
  runs; it should be paid for once. A re-extracted, fuller body invalidates the
  entry rather than reusing a summary written from a stub.
- `run.py --test-llm` — one tiny call to prove key and model before spending a
  whole run.
- Per-batch failure isolation: a failed batch falls back to extractive
  summaries for those items and the run continues. Items the model silently
  omits are detected and also fall back, so no blank cards are published.

### Ordering bug found and fixed

Priority was computed in stage 4, but the LLM's importance score only arrives
in stage 5 — so the model's judgement was being recorded and then ignored, and
the ranking would still have been the keyword heuristic's. Extracted
`s4_corroborate.score_priority()` / `rescore()`, and `run.py` now recomputes
priority after stage 5.

This is the whole point of Step 2, and it would have looked like it was working.

### Next action

Set `GEMINI_API_KEY`, run `python run.py --test-llm`, then a full
`python run.py`. Watch whether importance 1 correctly demotes the RBI treasury
auction items that the keyword heuristic ranked too highly.

---

## 2026-08-24 — Step 2 DONE, live against a real key

**Status:** the skeleton runs end to end with real LLM summarisation. Working.

### Run numbers

| Metric | Value |
|---|---|
| Gemini calls | **9** (100-item shortlist, batch 12) |
| Items summarised | 98 generated, 2 fell back |
| Batch failures | 0 |
| Wall time, stage 5 | ~7 min |
| Published | 40, balanced 5 / 12 / 12 / 11 |
| Claims verified at the gate | 100/100 entailed, 0 rejected |

### The test Step 2 existed to pass

All seven routine RBI items — treasury-bill auctions, VRRR auctions, weekly
statistical supplements, forex-swap data — scored **interest 0** from the model
and dropped off the page entirely. One of them had been holding the CRITICAL
slot under the keyword heuristic. The top item is now a 4-outlet TikTok/DOJ
settlement, followed by real security reporting.

The LLM importance score does what the keyword list could not.

### Fixed this session

- **`gemini-2.5-flash` is retired for new projects** (404 naming its
  replacement). Pinned to `gemini-3.6-flash`. Deliberately *not*
  `gemini-flash-latest`: this pipeline depends on a response schema, and a
  floating alias can shift behaviour silently, whereas a pinned model that
  retires fails loudly — exactly as it did here.
- **Stage 5 was summarising all ~315 scored items** (~27 calls) when only 40
  publish. Added `llm.max_items_summarized: 100` — a shortlist wide enough that
  the model can still reorder the ranking, but not so wide it pays for items
  that will never be read. **27 calls -> 9.** Rejected items are never sent.
- **`.env` support** (`pipeline/config.py`), gitignored, with `.env.example`.
  Real environment variables still win, so the Actions secret overrides it.
  Loader tolerates quotes, spaces, `export ` prefixes and comments.

### Gate proven, not assumed

`0 rejected` is indistinguishable from a gate that does nothing, so it was
tested against a deliberately fabricated support span: the invented quote was
classified `contradicted` and the item rejected, while a genuine quote passed.
The gate works.

### Next action

Step 3 (NLI entailment) is now lower value than expected — the span check
already catches invented quotes, and the model is honouring the verbatim-span
instruction (100/100). Better next step is **Step 4: GitHub Actions**, then
Vercel. Revisit NLI only if paraphrased-but-unsupported claims start appearing.

---

## 2026-08-24 — Refocus, tags, search, quota ladder, cross-model judge

**Status:** working. Content refocused onto releases / attacks / tools, tags
and search on the site, and API usage brought under a hard, measured cap.

### The quota correction that drove everything

The AI Studio dashboard was misread twice, so recording it plainly:

* The **100%** on the usage chart is the **success-rate** series (right axis),
  not quota consumed. 12 requests, all successful.
* The real constraint is on the *Rate limits by model* table:
  **gemini-3.6-flash is 20 requests per DAY** on the free tier. A run at
  batch_size 12 used 9 calls — 45% of a day, every day.
* Earlier "~1,500 requests/day" came from third-party blogs. Google no longer
  publishes free-tier RPM/RPD in its docs at all; the dashboard is the only
  authority. That figure was wrong and is corrected everywhere.

### Model ladder — every entry probed live, not read from docs

| Model | RPM | RPD | Status |
|---|---|---|---|
| gemini-3.7-flash | 5 | 20 | works |
| gemini-3.6-flash | 5 | 20 | works |
| gemini-3.5-flash | 5 | 20 | works |
| gemini-3-flash-preview | 5 | 20 | works |
| gemini-3.5-flash-lite | 15 | 500 | works |
| gemini-3.1-flash-lite | 15 | 500 | works |
| gemma-4-31b-it | 30 | 14,400 | works, honours the schema; TPM 16K so batches of 6 |
| gemma-4-26b-a4b-it | 30 | 14,400 | works |
| gemini-2.5-flash, gemini-2.5-flash-lite | — | — | **HTTP 404, retired** despite the dashboard showing quota rows |

Combined ~29,880 requests/day against the ~5 this pipeline needs. The ladder
exists so a bad day degrades gracefully, not because the volume is wanted.

**Never trust the dashboard's model list** — it advertises quota for models
that 404 on call. Probe before adding a tier.

### Cost work

* batch_size 12 -> 20. A live batch of 12 used 18.4K tokens against a 250K TPM
  ceiling, so tokens were never the constraint — **requests** were.
  **9 calls/run -> 5.**
* Persistent per-model, per-UTC-day counter in `data/cache/usage.json`, with a
  15% reserve so an ad-hoc `--test-llm` is never locked out by a full run.
* RPM pacing by sleeping rather than failing.
* A 429 blocks that model and falls through to the next tier instead of
  retrying into a closed door.
* When everything is exhausted the run still completes — remaining items get
  extractive summaries, so a page always exists.

### New content sources

* **GitHub search** (stage 1b) — 39 repos/run. GitHub rejects
  `topic:a OR topic:b` outright ("contains only logical operators"), so each
  search names ONE topic; unauthenticated search is 10/min, hence 7s pacing.
  Optional `GITHUB_TOKEN` raises it to 30/min.
* **Bluesky** (stage 1c) — built, then **disabled by default** on evidence:
  `searchPosts` returns 403 without auth, and the seed accounts' newest posts
  were 149h, 96h and 13,263h old. Author feeds are too thin for a 48h window.
* **X/Twitter** — not used. Free tier ended for new developers Feb 2026;
  reads are billed per post.
* 10 dead feeds removed (404/403/429/malformed), 7 probed replacements added:
  Talos, WeLiveSecurity, Malwarebytes, Elastic Security Labs, GitHub Security
  Lab, OALabs, NVIDIA Dev. Reddit feeds all 429 and were dropped.

### Summaries and tags

* Schema extended: `summary` now 3-5 sentences carrying the complete idea,
  plus `key_facts` (concrete "label: value" specifics) and `bottom_line`
  (the single consequential sentence). `key_facts` is deliberately optional —
  requiring it makes the model invent specifics for stories that have none.
* Controlled tag vocabulary (31 tags, `config/tags.yaml`). Model tags are
  intersected with the vocabulary and unioned with keyword rules; invented
  tags are dropped so facets never fragment.

### Bug: 24 of 40 published cards had no model summary

The shortlist picked the global top 100 by score, but stage 7 fills a
**per-category** quota — so items published only to fill a quiet section had
never been sent to the model. `_shortlist` is now category-aware
(1.5x the publish quota per category, then global fill).

### Stage 6b — independent cross-model judge

Stage 6 proves a quote exists in the source. It cannot catch a claim that
misreads a real quote. A second model, deliberately the *cheapest tier with
quota* rather than the best, re-checks each claim against the source:
contradicted -> rejected, unsupported -> shown as `disputed`. A failed judge
never rejects anything; unjudged is the safe state.

### Site rebuilt for reading

Serif body at 18.5px/1.72 on a 720px measure, search with highlighting, tag
facets that only offer filters returning results, and a bottom-line callout.
The control bar retracts on scroll-down and returns on scroll-up, and the tag
drawer is collapsed by default — 25 chips in a sticky header covered the
article text.

### Next action

Verify the judge on a live run, then Step 4 (GitHub Actions) and Step 5
(Vercel). A second Google account is available as a spare key if ever needed;
not wired, and not currently necessary.

---

## 2026-08-24 (overnight) — two audits, thirteen defects, site rebuilt

Two agents were run: one hunting API-key leak paths, one hunting correctness
bugs. Between them they found things no amount of re-reading had.

### The worst bug in the project so far

`config/interests.yaml` still had a `cybersecurity:` boost key after the
categories were split into `cyber_attacks` and `cyber_tools`. No code path
ever looks up `cybersecurity`, so the entire 15-term security keyword list
was dead config.

Interest carries the heaviest weight (0.45), so:

| headline | before | after |
|---|---|---|
| "Actively exploited zero-day RCE ransomware CVE-2026-1" | **12** | **100** |
| "Vendor publishes quarterly transparency report" | 12 | 12 |

Worse than the handicap: within the two cyber sections the interest term was
a *constant*, so ranking there collapsed to trust+corroboration and a routine
vendor blog sorted identically to an exploited-in-the-wild CVE.
`critical_signals` is a separate top-level key and still worked, which is
exactly what masked it.

**Lesson, now a test:** every `boost` key must name a real category, and
every category must have a profile. A config key nothing reads is the most
expensive kind of bug here, because the feature looks present.

### Crash-class

- **A Gemini safety block killed the whole run.** `data["candidates"][0]` and
  `candidate["content"]` were dereferenced unguarded; Gemini omits both on
  SAFETY/MAX_TOKENS and omits `candidates` entirely when the prompt is
  blocked. The KeyError escaped every caller and aborted the run *after*
  stages 1-5 had spent their budget. Feeds are untrusted input, so a safety
  block is routine here — it now degrades that batch.
- **Two self-inflicted `UnboundLocalError`s.** A function-scope `import`
  makes the name local to the WHOLE function, so an earlier use raises at
  runtime. It hit `s7_publish` (publish broken entirely) and then `run.py`
  (`s8_notify`, after a full successful run). Neither was caught by syntax
  checks, module imports, or 68 tests.
  **The guard I wrote for it was fake** — it checked "used before the import
  line", but line order was never the issue. Proven fake by reintroducing the
  bug and watching the test pass. Rewritten to detect the real smell: a name
  imported at module scope AND re-imported inside a function. Verified by
  watching it fail, then pass.

### Data-quality

- **Telegram timestamps were on the wrong posts.** Text divs and `<time>`
  tags were matched independently and zipped with `stamps[-len(blocks):]`,
  which only works if every extra stamp precedes the first text block. A
  media-only post or a reply desyncs everything after it — silently, since
  when the counts match the slice is a no-op. Now parsed per message.
- **Corroboration counted reach, not independence.** One breach story showed
  26 "outlets"; 21 were Yahoo Finance, Crypto Briefing, The Malone Telegram
  and similar republishing one release. That count is what earns `verified`.
  Syndication, press-release distributors and unmapped `.publisher` domains
  now collapse to one vote each. **26 -> 6.**
- **17 of 40 cards had no model summary**, and their keyword-interest median
  was *higher* than the summarised ones. Structural: the shortlist ranks on
  the pre-LLM blend, then the model's importance score reorders and promotes
  items that were never sent to it. Widening the shortlist cannot fix a
  promotion that happens after the shortlist is chosen — added a bounded
  `top_up()` pass instead.

### Also fixed

Digest sliced 10 items from unsorted order (a CRITICAL zero-day could lose to
ten routine market items) · budget ignored `blocked` when quota accounting was
off, spinning forever · `sources[0]` could be an aggregator stub under another
outlet's headline · an empty article body was recorded as "the model
fabricated its quote" · `--stop-after` shadowed the judge entirely ·
`rescore()` left stale trace lines so cards showed contradictory
escalate/demote pairs · `www.`-prefixed shorteners never resolved · the seen
store pruned by hex order rather than age.

### Security

`.env` was readable by every local account (inherited ACL) — gitignoring
protects the remote, not the filesystem. Locked, with a warning if it
regresses.

Three conditional key-leak paths shared one root cause: a token containing a
control character makes `http.client` raise `ValueError("Invalid header value
%r")` with the FULL header in the message, which is then logged. One
sanitiser at load closes all three. Audited clean otherwise: nothing in
`data/`, the published site, git history, or the Actions workflow.

### Site

Filters in the URL (shareable) · sort by priority/newest/most-sources ·
keyboard navigation (j/k, o, /, Esc, ?) · **claims shown** with pass/fail
marks rather than the page merely asserting "verified" · per-section lookback
labels so a five-day-old tools item does not read as stale · live region,
focus styles, skip link, print stylesheet.

### Tests: 42 -> 75

### Next action

Two research agents are running on presentation and recall. After that: the
GitHub secret and one manual Actions run remain the only blockers to Vercel.
