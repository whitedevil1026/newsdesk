"""Stage 7 — publish.

Writes the single artefact the website reads. Rejected items are written
to a separate audit file, never to the site, so a bad call can always be
traced after the fact.
"""

from __future__ import annotations

from pathlib import Path

from .config import DATA_DIR, SITE_DIR, settings
from .models import Item, Priority, Verdict
from .utils import log, now_utc, snapshot

NEWS_JSON = DATA_DIR / "news.json"
REJECTS_JSON = DATA_DIR / "rejected.json"

_ORDER = {Priority.CRITICAL: 0, Priority.IMPORTANT: 1, Priority.MINOR: 2}


def _apply_quotas(ranked: list, wcfg: dict) -> list:
    """Cap each category, then refill spare slots by global rank.

    A straight top-N cut let one high-volume wire take 39 of 40 slots on a
    live run. Categories are capped first so every section has something,
    then whatever is left over is filled by the best remaining items.
    """
    total = wcfg["max_items_published"]
    cap = wcfg["per_category_max"]

    chosen, counts = [], {}
    for item in ranked:                      # already ranked priority-then-blend
        n = counts.get(item.category, 0)
        if n < cap and len(chosen) < total:
            chosen.append(item)
            counts[item.category] = n + 1

    if len(chosen) < total:                  # spare capacity: best of the rest
        picked = {id(i) for i in chosen}
        for item in ranked:
            if len(chosen) >= total:
                break
            if id(item) not in picked:
                chosen.append(item)

    chosen.sort(key=lambda i: (_ORDER[i.priority], -i.blend))
    return chosen


def run(items: list[Item], dry_run: bool = False) -> dict:
    cfg = settings()

    live = [i for i in items if i.verdict is not Verdict.REJECTED]
    dead = [i for i in items if i.verdict is Verdict.REJECTED]

    # Within a priority band, rank by the blended score, not trust alone —
    # sorting by trust buried every interesting story under official notices.
    live.sort(key=lambda i: (
        _ORDER[i.priority],
        -i.blend,
        -(i.published.timestamp() if i.published else 0),
    ))
    live = _apply_quotas(live, cfg["window"])

    payload = {
        "generated_at": now_utc().isoformat(),
        "window_hours": cfg["window"]["lookback_hours"],
        "counts": {
            "published": len(live),
            "rejected": len(dead),
            "critical": sum(1 for i in live if i.priority is Priority.CRITICAL),
            "important": sum(1 for i in live if i.priority is Priority.IMPORTANT),
            "verified": sum(1 for i in live if i.verdict is Verdict.VERIFIED),
            "corroborated": sum(1 for i in live if i.verdict is Verdict.CORROBORATED),
            "established": sum(1 for i in live if i.verdict is Verdict.ESTABLISHED),
            "unverified": sum(1 for i in live if i.verdict is Verdict.UNVERIFIED),
            "disputed": sum(1 for i in live if i.verdict is Verdict.DISPUTED),
        },
        "items": [i.to_json() for i in live],
    }

    if dry_run:
        log("publish", f"DRY RUN - would publish {len(live)}, reject {len(dead)}")
        return payload

    snapshot(payload, NEWS_JSON, "news")
    # The site is served as a plain static directory, so its data file lives
    # beside index.html rather than being fetched from ../data.
    snapshot(payload, SITE_DIR / "news.json", "site copy")
    snapshot({"generated_at": payload["generated_at"],
              "items": [i.to_json() for i in dead]}, REJECTS_JSON, "rejects")

    # Mark published sources as seen so tomorrow's run skips them.
    from .s2_clean import save_seen
    from .models import canonical_url
    import hashlib
    uids = {
        hashlib.sha1(canonical_url(s["url"]).encode()).hexdigest()[:16]
        for i in live for s in i.sources
    }
    save_seen(uids)

    log("publish", f"{len(live)} published, {len(dead)} rejected, {len(uids)} urls marked seen")
    return payload
