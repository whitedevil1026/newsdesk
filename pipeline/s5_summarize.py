"""Stage 5 — summarise, write the one-liner, score importance.

Provider is chosen in settings.yaml:

  none    deterministic extractive fallback; the whole pipeline stays
          testable with no API key at all
  gemini  Gemini Developer API, free tier

Whatever the provider, the contract is the same: summary, one_liner,
importance 1-5, and claims each carrying the verbatim span that supports
them. Claims exist so stage 6 can verify them — a provider that cannot
produce them cannot be verified, and its output must stay untrusted.

Summaries are cached by cluster identity. The dashboard shows a rolling
48h window, so the same article legitimately appears in several runs; it
should be paid for once.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .config import CACHE_DIR, api_key, interests, settings
from . import tagging
from .budget import Budget
from .models import Claim, Item, Verdict
from .utils import log, truncate

SUMMARY_CACHE = CACHE_DIR / "summaries.json"


# ---------------------------------------------------------------- cache ----

def _load_cache() -> dict[str, dict]:
    if SUMMARY_CACHE.exists():
        try:
            return json.loads(SUMMARY_CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("summarize", "! summary cache corrupt, starting fresh")
    return {}


def _save_cache(cache: dict[str, dict]) -> None:
    # Bounded: keep the most recent entries so the file cannot grow forever.
    trimmed = dict(list(cache.items())[-5000:])
    SUMMARY_CACHE.write_text(json.dumps(trimmed, ensure_ascii=False),
                             encoding="utf-8")


def _apply(item: Item, payload: dict, source: str, body: str = "") -> None:
    item.summary_source = "model"
    item.summary = payload.get("summary", "")
    item.one_liner = payload.get("one_liner", "")
    item.bottom_line = payload.get("bottom_line", "")
    # Cap at five: past that the fact list competes with the summary instead
    # of supporting it.
    item.key_facts = [f.strip() for f in payload.get("key_facts", [])
                      if isinstance(f, str) and f.strip()][:5]
    tagging.apply(item, body, payload.get("tags"))
    # A real summary replaces an extractive one, so drop the marker or the
    # top-up pass would keep re-selecting the same items forever.
    item.trace = [t for t in item.trace
                  if "extractive" not in t and "heuristic" not in t]
    item.claims = [
        Claim(text=c.get("text", ""), support_span=c.get("support_span", ""))
        for c in payload.get("claims", [])
    ]
    imp = payload.get("importance")
    if isinstance(imp, int) and 1 <= imp <= 5:
        # The model's judgement replaces the keyword heuristic outright. The
        # heuristic cannot tell a central-bank penalty from a scheduled
        # treasury auction; this is the whole reason the LLM stage exists.
        item.interest_score = int((imp - 1) / 4 * 100)
        item.note(f"importance {imp}/5 from {source}")


# ----------------------------------------------------------- fallback ----

def _heuristic(item: Item, body: str) -> None:
    """No-LLM fallback. Honest about what it is: extractive, not abstractive."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body)
                 if len(s.strip()) > 40]
    item.summary = truncate(" ".join(sentences[:3]), 480) if sentences \
        else "(no article text available — headline only)"

    bits = []
    if item.primary_links:
        bits.append("anchored to an official source")
    if item.corroboration > 1:
        bits.append(f"carried by {item.corroboration} outlets")
    item.one_liner = f"{item.category.replace('_', ' ').title()}: " + \
                     (", ".join(bits) if bits else "single report, unconfirmed") + "."

    # These are the article's own sentences quoted back, so "entailed" is
    # true by construction and means nothing. Keeping them under the heading
    # "Claims checked against the source" would have read as verification on
    # most of the page once it started publishing everything in the window.
    # No check happened here, so no claims are reported.
    item.claims = []
    item.bottom_line = ""      # only the model can judge what the point is
    tagging.apply(item, body)  # keyword tags still work with no model
    item.summary_source = "extract"
    item.note("summary: the article's opening sentences, not model-written "
              "and not fact-checked")


def _fallback_all(items: list[Item], bodies: dict[str, str], why: str) -> list[Item]:
    log("summarize", why)
    for item in items:
        _heuristic(item, bodies.get(item.cluster_key, ""))
    return items


def _shortlist(items: list[Item], limit: int, per_category: int,
               multiplier: float = 2.0) -> tuple[list[Item], list[Item]]:
    """Split into the items worth an API call and the rest.

    Category-aware, and it has to be. Stage 7 fills a per-category quota, so
    a purely global top-N shortlist starves the quieter sections: on a live
    run that left 24 of 40 published cards with no model summary at all,
    because they were only published to fill their category's quota.

    So each category gets its own allocation first — generously, since the
    model's importance score can reorder within a category — and any slots
    left over are filled by global rank.

    Rejected items are never sent: paying to summarise something the gate has
    already thrown out is pure waste.
    """
    live = [i for i in items if i.verdict is not Verdict.REJECTED]
    dead = [i for i in items if i.verdict is Verdict.REJECTED]

    by_cat: dict[str, list[Item]] = {}
    for item in sorted(live, key=lambda i: -i.blend):
        by_cat.setdefault(item.category, []).append(item)

    allocation = max(1, int(per_category * multiplier))
    chosen: list[Item] = []
    picked: set[int] = set()
    # ROUND-ROBIN, not category-by-category. Filling one category's whole
    # allocation before starting the next only works while every allocation
    # fits inside the limit; the moment it does not, the categories that
    # happen to come first eat the entire budget and the rest get nothing.
    # With five categories, a 150 budget and a 60 allocation that is exactly
    # what happened - three categories took all 150 and markets and
    # india_world shipped with no model summaries at all.
    for rank in range(allocation):
        if len(chosen) >= limit:
            break
        for group in by_cat.values():
            if len(chosen) >= limit:
                break
            if rank < len(group):
                chosen.append(group[rank])
                picked.add(id(group[rank]))

    for item in sorted(live, key=lambda i: -i.blend):
        if len(chosen) >= limit:
            break
        if id(item) not in picked:
            chosen.append(item)
            picked.add(id(item))

    rest = [i for i in live if id(i) not in picked]
    return chosen, rest + dead



BULK_DOMAINS = {"bsky.app", "github.com"}


def _is_bulk(item) -> bool:
    """Should this item be summarised by a cheap, high-quota model?

    Two cases, both the user's point: material that does not update daily
    (social posts, repository listings) and material ranked too low to be
    read closely. Neither justifies spending one of twenty daily flagship
    requests, but both are still worth collecting and summarising.
    """
    from .models import Priority

    if item is None:
        return False
    if any(s.get("domain") in BULK_DOMAINS for s in item.sources):
        return True
    return item.priority is Priority.MINOR


def _generate(batch, budget, key, profile, tag_list, llm_gemini,
              n: int, total: int, prefer: str = "premium"):
    """Try each model with quota until one serves this batch.

    A model can fail for reasons that say nothing about the batch — a 503
    overload, a 429 quota wall. Falling straight back to an extractive
    summary in that case wastes an entire ladder of working models, which is
    exactly what happened on a live run when gemini-3.7-flash returned 503
    while seven other models sat idle.

    Returns (results, model_name), or (None, "") if every tier is spent.
    """
    while True:
        tier = budget.next_model(prefer)
        if tier is None:
            return None, ""

        # A tier with a tighter token ceiling takes the batch in slices.
        chunk = tier.batch_size or len(batch)
        slices = [batch[i:i + chunk] for i in range(0, len(batch), chunk)]

        try:
            results = []
            for part in slices:
                budget.wait(tier)
                payload = [{k: b[k] for k in ("id", "title", "source", "body")}
                           for b in part]
                got = llm_gemini.call(tier.model, key, profile, payload,
                                      tags=tag_list)
                # Record AFTER success. Debiting first meant a 429 or 503 —
                # which returns nothing — still spent the local allowance,
                # so the counter drifted pessimistic against the provider.
                budget.record(tier)
                results += got
            return results, tier.model

        except llm_gemini.RateLimited as exc:
            # Quota refusals last until the quota resets, so remember them.
            budget.block(tier, f"rate limited — {str(exc)[:60]}", persist=True)
        except llm_gemini.GeminiError as exc:
            # Config errors (bad key, retired model) are permanent for this
            # model but say nothing about the others, so block and move on.
            log("summarize", f"! batch {n}/{total} on {tier.model}: "
                             f"{str(exc)[:90]}")
            budget.block(tier, "call failed")

def top_up(items: list[Item], bodies: dict[str, str]) -> list[Item]:
    """Summarise items that will PUBLISH but were never sent to the model.

    The shortlist is ranked on the pre-LLM blend, but the model's importance
    score then reorders everything: demoting a shortlisted item lets an
    unshortlisted one rise into the publish set, arriving with only an
    extractive summary. On a live run that left 17 of 40 published cards
    without a real summary, and their keyword-interest median was HIGHER
    than the summarised ones — these were not filler.

    Widening the shortlist cannot fix this, because the promotion happens
    after the shortlist is chosen. Only a second pass can. It is bounded to
    the publish cap, so the worst case is two extra calls.
    """
    from .config import settings as _settings
    from .s7_publish import _apply_quotas

    cfg = _settings()
    if cfg["llm"]["provider"] == "none" or not api_key():
        return items

    live = [i for i in items if i.verdict is not Verdict.REJECTED]
    live.sort(key=lambda i: (i.priority.value != "critical", -i.blend))
    will_publish = _apply_quotas(live, cfg["window"])

    gaps = [i for i in will_publish
            if any("extractive" in t or "heuristic" in t for t in i.trace)]
    if not gaps:
        return items

    log("summarize", f"top-up: {len(gaps)} publishable items have no model "
                     f"summary")
    # run() mutates the Item objects in place and returns the list it was
    # given, so the ORIGINAL list must be returned here — returning run()'s
    # value would silently reduce the pipeline to just the gap items.
    run(gaps, bodies, _is_top_up=True)
    return items


# --------------------------------------------------------------- main ----

def run(items: list[Item], bodies: dict[str, str],
        _is_top_up: bool = False) -> list[Item]:
    cfg = settings()["llm"]
    provider = cfg["provider"]

    if provider == "none":
        return _fallback_all(items, bodies,
                             f"provider=none — extractive fallback for {len(items)} items")

    key = api_key()
    if not key:
        return _fallback_all(
            items, bodies,
            f"! {cfg['api_key_env']} not set — falling back to extractive")

    if provider != "gemini":
        raise NotImplementedError(
            f"provider '{provider}' is not implemented. Use 'gemini' or 'none'.")

    from . import llm_gemini

    cache = _load_cache()
    profile = interests()["profile"]

    # Only a shortlist is worth paying for. Items outside it still get an
    # extractive summary so nothing is left blank, but they are far below the
    # publish cut and the model's opinion of them would never be read.
    if _is_top_up:
        shortlist, rest = items, []      # caller already chose the exact set
    else:
        # The per-category allocation used to be the PUBLISH quota. That
        # coupling broke the moment the page started publishing everything
        # in the window: per_category_max is now a guard against one wire
        # owning the page, not a page size, so reading it here asked the
        # model for hundreds of summaries the budget cannot pay for.
        # The shortlist is a spending decision, so derive it from the
        # spending limit.
        cats = len({i.category for i in items}) or 1
        per_cat = max(1, cfg["max_items_summarized"] // cats)
        shortlist, rest = _shortlist(
            items, cfg["max_items_summarized"], per_cat,
            cfg.get("shortlist_multiplier", 2.0))
    if rest:
        log("summarize", f"shortlist {len(shortlist)} to the model, "
                         f"{len(rest)} extractive (below the publish cut)")
        for item in rest:
            _heuristic(item, bodies.get(item.cluster_key, ""))

    pending: list[dict] = []
    cached = 0
    for item in shortlist:
        body = bodies.get(item.cluster_key, "")
        # Cache key includes the body so a re-extracted, fuller article gets
        # a fresh summary rather than reusing one written from a stub.
        # Try the cluster key first, then every member article's uid. The
        # cluster key moves when membership changes — a story picked up by a
        # second outlet reseeds it — so a single-key lookup missed summaries
        # already paid for. Measured hit rate was 21% against an expected
        # ~50% for a rolling window run daily.
        ckey = f"{item.cluster_key}:{len(body)}"
        hit = next((k for k in [ckey] + [f"{a}:{len(body)}" for a in item.alt_keys]
                    if k in cache), None)
        if hit:
            _apply(item, cache[hit], "cache", body)
            cache[ckey] = cache[hit]        # re-file under the current key
            cached += 1
            continue
        pending.append({"id": item.cluster_key, "title": item.title,
                        "source": item.sources[0]["name"] if item.sources else "",
                        "body": body, "_item": item, "_ckey": ckey})

    log("summarize", f"{cached} from cache, {len(pending)} to generate")
    if not pending:
        return items

    by_id = {p["id"]: p for p in pending}
    size = cfg["batch_size"]
    batches = [pending[i:i + size] for i in range(0, len(pending), size)]

    # Budget gate. Capacity is summed across the model ladder; if it cannot
    # cover every batch, the run is trimmed up front rather than dying
    # part-way through. Batches are already ordered best-first, so the ones
    # that get dropped are the least important.
    budget = Budget()
    capacity = budget.capacity()
    if capacity < len(batches):
        log("summarize", f"! quota covers {capacity}/{len(batches)} batches")
        log("budget", budget.report())
        for b in [x for batch in batches[capacity:] for x in batch]:
            _heuristic(b["_item"], b["body"])
            b["_item"].note("beyond API quota — extractive fallback")
        batches = batches[:capacity]

    if not batches:
        log("summarize", f"! no calls permitted — {budget.stopped_reason}")
        _save_cache(cache)
        return items

    log("summarize", f"{len(batches)} call(s), batch size {size}")
    tag_list = ", ".join(sorted(tagging.vocabulary()))
    done = failed = 0

    for n, batch in enumerate(batches, 1):
        # A batch is "bulk" when most of it is low-frequency or low-ranked
        # material. Routing it to the cheap tier keeps the flagship quota for
        # the stories at the top of the page.
        bulk = sum(_is_bulk(b["_item"]) for b in batch) > len(batch) / 2
        results, used = _generate(batch, budget, key, profile, tag_list,
                                  llm_gemini, n, len(batches),
                                  prefer="bulk" if bulk else "premium")
        if results is None:
            for b in batch:
                _heuristic(b["_item"], b["body"])
                b["_item"].note("no model could serve this batch — extractive")
                failed += 1
            continue

        seen_ids = set()
        for res in results:
            rid = res.get("id")
            src = by_id.get(rid)
            if not src:
                log("summarize", f"! unknown id in response: {rid!r}")
                continue
            seen_ids.add(rid)
            _apply(src["_item"], res, used, src["body"])
            cache[src["_ckey"]] = res
            done += 1

        # A model that silently drops an item would leave it with no summary
        # at all; catch that rather than publishing a blank card.
        for b in batch:
            if b["id"] not in seen_ids:
                _heuristic(b["_item"], b["body"])
                b["_item"].note("omitted by the model — extractive fallback")
                failed += 1

        log("summarize", f"batch {n}/{len(batches)} ok via {used} "
                         f"({len(seen_ids)} items)")

    _save_cache(cache)
    log("summarize", f"{done} generated, {cached} cached, {failed} fell back")
    log("budget", budget.report())
    return items
