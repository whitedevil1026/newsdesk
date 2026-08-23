# Backlog

Things agreed but deliberately not built yet, so they are not lost.
See [PLAN.md](PLAN.md) for the design and [PROGRESS.md](PROGRESS.md) for state.

---

## Deferred — after the site is complete

### API usage tracking exported to Google Sheets

**Asked for:** 2026-08-24. Explicitly *not urgent* — do it once the site is
finished.

Track every run in a spreadsheet: when it ran, how many requests were made,
to which model, to which API, and the outcome. The user wants this visible in
a sheet rather than only in a local JSON file.

**What already exists to build on:** `data/cache/usage.json` holds per-model,
per-UTC-day counts and the `_blocked` list. That is the right raw material,
but it is a snapshot, not a log — it records totals, not events. A per-run
event log needs adding first.

**Sketch, not a decision:**

1. Append a row per run to a local `data/usage_log.csv`: timestamp, run id,
   stage, api, model, calls, tokens if available, outcome (ok / 503 / 429),
   items summarised, items judged.
2. Push to Sheets. Two credible routes, and the choice matters:
   - **Service account + `gspread`** — proper API, needs a JSON key file and
     the sheet shared with the service-account email. Robust, more setup.
   - **Apps Script web app endpoint** — a `doPost` that appends a row; the
     pipeline just POSTs JSON. No client library, no key file, but the
     endpoint URL is a secret and must live in `.env`.
3. Verify quota before recommending either: the Sheets API free tier is
   generous but not unlimited, and this project's daily volume is tiny.

**Open question to settle before building:** should the sheet be the log
itself (append-only, one row per API call) or a daily rollup? The user asked
to see "how many calls to which api", which reads like per-call detail —
worth confirming, because append-only grows without bound and a rollup does
not.

**Do not start this until Steps 4 (GitHub Actions) and 5 (Vercel) are done.**
