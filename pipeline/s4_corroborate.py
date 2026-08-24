"""Stage 4 — corroboration and trust scoring.

Answers the first validation question: is the STORY real? Purely mechanical
signals, no model involved, so it is free, fast and auditable.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from .config import interests, settings
from .models import Cluster, Item, Priority, Verdict
from .utils import log, now_utc

# Domains that syndicate the same wire copy. Counting them as independent
# outlets would inflate corroboration, so they collapse to one vote.
WIRE_GROUPS = {
    "reuters": {"reuters.com", "in.reuters.com"},
    "ap": {"apnews.com", "ap.org"},
    "pti": {"ptinews.com"},
    "afp": {"afp.com"},
}

# Hosts that indicate an official, checkable artifact behind the story.
PRIMARY_HOSTS = (
    "cisa.gov", "nvd.nist.gov", "cve.org", "cve.mitre.org", "sec.gov",
    "rbi.org.in", "sebi.gov.in", "pib.gov.in", "nseindia.com", "bseindia.com",
    "arxiv.org", "github.com/advisories", "msrc.microsoft.com",
    "chromereleases.googleblog.com", "openai.com", "anthropic.com", "blog.google",
)


def _independent_outlets(cluster: Cluster) -> int:
    domains = {a.domain for a in cluster.articles}
    for members in WIRE_GROUPS.values():
        hit = domains & members
        if len(hit) > 1:
            domains -= hit
            domains.add(next(iter(hit)))      # collapse to a single vote
    return len(domains)


def _primary_links(cluster: Cluster) -> list[str]:
    links = [a.url for a in cluster.articles
             if a.is_primary or any(h in a.url for h in PRIMARY_HOSTS)]
    return sorted(set(links))


def _interest_score(cluster: Cluster, icfg: dict) -> int:
    """Keyword relevance, 0-100. Replaced by the LLM's score at Step 3.

    A saturating curve rather than a linear count: the difference between
    zero hits and one is large, between four hits and five is negligible.
    Title hits weigh double because a term in the headline says what the
    story is about; the same term in the body may be an aside.
    """
    prof = interests()
    terms = [t.lower() for t in prof["boost"].get(cluster.lead.category, [])]
    titles = " ".join(a.title for a in cluster.articles).lower()
    bodies = " ".join(a.summary_raw for a in cluster.articles).lower()

    weighted = 0.0
    for term in terms:
        if term in titles:
            weighted += icfg["title_weight"]
        elif term in bodies:
            weighted += icfg["body_weight"]

    saturated = 100 * (1 - math.exp(-icfg["saturation_k"] * weighted))
    score = icfg["category_base"] + saturated

    # Deliberately NO primary-source bonus: an official source is a reason to
    # trust a story, not a reason to care about it.
    if any(m in titles for m in icfg["routine_markers"]):
        score -= icfg["routine_penalty"]

    return int(max(0, min(100, score)))


def _is_critical_signal(cluster: Cluster) -> str | None:
    """Terms that make a story critical on their own merits."""
    hay = " ".join(f"{a.title} {a.summary_raw}" for a in cluster.articles).lower()
    for sig in interests().get("critical_signals", []):
        if sig.lower() in hay:
            return sig
    return None


def _feed_window(article, cfg: dict) -> int:
    """The lookback this article was actually harvested under."""
    from .s1_harvest import window_for
    return window_for(article.category, cfg,
                      {"window_hours": article.window_hours}
                      if article.window_hours else None)


def _best_date(cluster: Cluster) -> datetime | None:
    dates = [a.published for a in cluster.articles if a.published]
    return min(dates) if dates else None      # earliest = when it broke


def run(clusters: list[Cluster]) -> list[Item]:
    cfg = settings()
    tcfg, pcfg = cfg["trust"], cfg["priority"]
    # Must mirror the harvest windows exactly, or the stale-drop below throws
    # away everything the wider category windows just admitted.
    from .s1_harvest import window_for
    cutoffs = {}

    items: list[Item] = []
    rejected = 0

    for cl in clusters:
        lead = cl.lead
        outlets = _independent_outlets(cl)
        primaries = _primary_links(cl)
        published = _best_date(cl)

        item = Item(
            cluster_key=cl.key,
            title=lead.title,
            category=lead.category,
            corroboration=outlets,
            primary_links=primaries,
            published=published,
            # Aggregators sort LAST, matching Cluster.lead. sources[0] is what
            # the card links to, the console prints and stage 5 tells the
            # model — so without this guard a card could show one outlet's
            # headline above another outlet's redirect stub.
            sources=[{"name": a.source, "url": a.url, "domain": a.domain,
                      "tier": a.tier}
                     for a in sorted(cl.articles,
                                     key=lambda a: (a.is_aggregator, a.tier,
                                                    a.domain))],
        )

        # --- hard gate: recycled or out-of-window content ------------------
        # Use the WIDEST window any article in the cluster was harvested
        # under. A slow research blog admitted at 720h must not then be
        # dropped because a wire story in the same cluster only gets 48h.
        span = max(_feed_window(a, cfg) for a in cl.articles)
        cutoff = now_utc() - timedelta(hours=span)
        if tcfg["hard_drop"]["outside_window"] and published and published < cutoff:
            item.verdict = Verdict.REJECTED
            item.reject_reason = f"published {published.date()} — outside lookback window"
            rejected += 1
            items.append(item)
            continue

        # --- trust score ---------------------------------------------------
        score = tcfg["tier_points"].get(lead.tier, 10)
        item.note(f"tier {lead.tier} (+{tcfg['tier_points'].get(lead.tier, 10)})")

        extra = min((outlets - 1) * tcfg["outlet_points"], tcfg["outlet_cap"])
        if extra:
            score += extra
            item.note(f"{outlets} independent outlets (+{extra})")

        if primaries:
            score += tcfg["primary_source_bonus"]
            item.note(f"primary source anchor (+{tcfg['primary_source_bonus']})")

        item.trust_score = max(0, min(100, score))

        # --- verdict: rules over the real signals, not a score threshold ----
        if primaries or outlets >= 3:
            item.verdict = Verdict.VERIFIED
            item.note("verified: "
                      + ("primary source artifact" if primaries
                         else f"{outlets} independent outlets"))
        elif outlets >= 2:
            item.verdict = Verdict.CORROBORATED
            item.note(f"corroborated by {outlets} independent outlets")
        elif lead.tier in ("A", "B"):
            item.verdict = Verdict.ESTABLISHED
            item.note(f"single outlet, established publication (tier {lead.tier})")
        else:
            item.verdict = Verdict.UNVERIFIED
            item.note(f"single tier-{lead.tier} source — treat with caution")

        # --- priority ---------------------------------------------------------
        item.interest_score = _interest_score(cl, cfg["interest"])
        item.critical_signal = _is_critical_signal(cl) or ""
        score_priority(item, pcfg)

        items.append(item)

    _cap_critical(items, pcfg)

    live = len(items) - rejected
    dist = {p.value: sum(1 for i in items if i.priority is p) for p in Priority}
    log("corroborate", f"{live} scored, {rejected} hard-rejected (stale)")
    log("corroborate", f"priority {dist}")
    return items


def _cap_critical(items: list[Item], pcfg: dict) -> None:
    """Keep the red badge rare enough to mean something.

    Without this, a busy news day floods the top of the page with CRITICAL
    and the signal is gone. Overflow demotes to IMPORTANT, lowest blend first.
    """
    live = [i for i in items if i.verdict is not Verdict.REJECTED]
    crit = [i for i in live if i.priority is Priority.CRITICAL]
    allowed = max(1, min(pcfg["max_critical"],
                         int(len(live) * pcfg["max_critical_share"]) or 1))
    if len(crit) <= allowed:
        return
    for item in sorted(crit, key=lambda i: -i.blend)[allowed:]:
        item.priority = Priority.IMPORTANT
        item.note(f"demoted: critical capped at {allowed}")
    log("corroborate", f"capped critical {len(crit)} -> {allowed}")


def score_priority(item: Item, pcfg: dict | None = None) -> None:
    """Compute blend and priority bucket for one item.

    Split out of ``run`` because stage 5 replaces ``interest_score`` with the
    model's importance judgement. Without recomputing afterwards, the LLM's
    opinion would be recorded and then ignored — the ranking would still be
    the keyword heuristic's.
    """
    pcfg = pcfg or settings()["priority"]

    corrob_norm = min(100, 25 + (item.corroboration - 1) * 25)
    blended = (
        pcfg["weights"]["trust"] * item.trust_score
        + pcfg["weights"]["interest"] * item.interest_score
        + pcfg["weights"]["corroboration"] * corrob_norm
    )
    item.blend = round(blended, 1)
    item.note(f"blend {blended:.0f} = trust {item.trust_score} / "
              f"interest {item.interest_score} / corrob {corrob_norm}")

    if blended >= pcfg["buckets"]["critical"]:
        item.priority = Priority.CRITICAL
    elif blended >= pcfg["buckets"]["important"]:
        item.priority = Priority.IMPORTANT
    else:
        item.priority = Priority.MINOR

    # A story can be critical on its own terms even with a modest blend — an
    # actively exploited zero-day carried by a single outlet still matters.
    if item.critical_signal and item.priority is not Priority.CRITICAL:
        item.priority = Priority.CRITICAL
        item.note(f"escalated to CRITICAL by signal: '{item.critical_signal}'")


def rescore(items: list[Item]) -> list[Item]:
    """Re-run priority after stage 5 has supplied real importance scores."""
    pcfg = settings()["priority"]
    for item in items:
        if item.verdict is not Verdict.REJECTED:
            item.trace = [t for t in item.trace if not t.startswith("blend ")]
            score_priority(item, pcfg)
    _cap_critical(items, pcfg)
    dist = {p.value: sum(1 for i in items if i.priority is p) for p in Priority}
    log("rescore", f"priority after LLM importance: {dist}")
    return items
