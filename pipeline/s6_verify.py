"""Stage 6 — the gate.

Answers the second validation question: is MY SUMMARY faithful to the
source?  Cheapest-first cascade, exactly as production hallucination
detection is normally built:

  1. span check   - is support_span actually present in the body?  (here, free)
  2. cross-model  - does a DIFFERENT model agree the claim follows
                    from the source?                                (stage 6b)

A local NLI model was planned as step 2 and dropped: it meant a 400MB
download to answer a question the judge in stage 6b already answers using
quota that is otherwise idle.

This stage stays deterministic and offline on purpose. It is the check that
still works when there is no API key, no quota, and no network.
"""

from __future__ import annotations

from .config import settings
from .models import Item, Verdict
from .utils import log


def _normalise(text: str) -> str:
    return " ".join(text.lower().split())


def _span_check(item: Item, body: str) -> None:
    """A support_span not present in the body means the quote was invented.

    Unless there IS no body. "We never fetched the article" and "the model
    invented its evidence" are different failures, and without this guard the
    first was recorded as the second — hard-rejecting the item with a
    reject_reason that blamed the model for a fetch that never happened.
    """
    hay = _normalise(body)
    if not hay:
        for claim in item.claims:
            if claim.status == "unchecked":
                claim.status = "neutral"       # unverifiable, not disproven
        item.note("no article text available — claims left unverified")
        return

    for claim in item.claims:
        if claim.status != "unchecked":
            continue
        span = _normalise(claim.support_span)
        if not span:
            claim.status = "neutral"
        elif span in hay:
            claim.status = "entailed"
        else:
            claim.status = "contradicted"      # fabricated quote — hard fail


def run(items: list[Item], bodies: dict[str, str]) -> list[Item]:
    cfg = settings()["verify"]
    tcfg = settings()["trust"]

    passed = rejected = 0
    for item in items:
        if item.verdict is Verdict.REJECTED:
            continue

        _span_check(item, bodies.get(item.cluster_key, ""))

        checked = [c for c in item.claims if c.status != "unchecked"]
        bad = [c for c in checked if c.status == "contradicted"]
        good = [c for c in checked if c.status == "entailed"]

        if bad and tcfg["hard_drop"]["contradicted_claim"]:
            item.verdict = Verdict.REJECTED
            item.reject_reason = f"{len(bad)} claim(s) not supported by the source text"
            rejected += 1
            continue

        ratio = len(good) / len(checked) if checked else 0.0
        item.note(f"claims entailed {len(good)}/{len(checked)} ({ratio:.0%})")
        if checked and ratio < cfg["min_entailed_ratio"]:
            item.note("weak grounding — kept but not eligible for CRITICAL")
            from .models import Priority
            if item.priority is Priority.CRITICAL:
                item.priority = Priority.IMPORTANT
        passed += 1

    log("verify", f"{passed} passed the gate, {rejected} rejected")
    return items
