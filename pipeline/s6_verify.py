"""Stage 6 — the gate.

Answers the second validation question: is MY SUMMARY faithful to the
source?  Cheapest-first cascade, exactly as production hallucination
detection is normally built:

  1. span check      - is support_span actually present in the body?   (free)
  2. NLI entailment  - local model, claim vs body                       (free, Step 4)
  3. LLM judge       - borderline cases only                            (Step 4)

Only step 1 is live in the skeleton.  It already catches the most common
failure: a model that invents its own supporting quote.
"""

from __future__ import annotations

from .config import settings
from .models import Item, Verdict
from .utils import log


def _normalise(text: str) -> str:
    return " ".join(text.lower().split())


def _span_check(item: Item, body: str) -> None:
    """A support_span not present in the body means the quote was invented."""
    hay = _normalise(body)
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

    if not cfg["enabled"]:
        log("verify", "verify.enabled=false - span check only, no NLI")

    passed = rejected = 0
    for item in items:
        if item.verdict is Verdict.REJECTED:
            continue

        _span_check(item, bodies.get(item.cluster_key, ""))

        if cfg["enabled"]:
            raise NotImplementedError(
                "NLI entailment not wired yet (Step 4). "
                "Set verify.enabled: false to run the skeleton."
            )

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
