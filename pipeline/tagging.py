"""Tagging against a controlled vocabulary.

Two sources feed the same fixed list:

  * the model, which picks tags in stage 5 (semantic, catches implication)
  * keyword rules here (deterministic, catches the obvious, works offline)

Union of the two, filtered to the vocabulary. Anything the model invents is
discarded — a free-form tag set fragments into "dfir" / "DFIR" / "forensics"
and search stops being trustworthy, which defeats the point of having tags.
"""

from __future__ import annotations

import re
from functools import lru_cache

from .config import CONFIG_DIR, _load
from .models import Item


@lru_cache(maxsize=1)
def vocabulary() -> frozenset[str]:
    return frozenset(_load("tags.yaml")["vocabulary"])


@lru_cache(maxsize=1)
def _rules() -> list[tuple[str, re.Pattern]]:
    """Compile each tag's keywords into one alternation pattern.

    Terms are matched on word boundaries, so the old hand-written space
    guards in the config (" ai ", "rat ") are no longer needed.
    """
    out = []
    for tag, terms in _load("tags.yaml")["rules"].items():
        if tag not in vocabulary():
            continue
        parts = []
        for term in sorted(terms, key=len, reverse=True):
            term = term.strip()
            if not term:
                continue
            esc = re.escape(term)
            # Word boundaries, not bare substrings. Without them "rce" matched
            # COMMERCE and a Shein IPO story was tagged `exploit`; "poc" would
            # match "pocket", " ai " needed a hand-written space guard.
            # \b only asserts next to a word character, so add it per end only
            # when the term actually starts or ends with one.
            left = r"\b" if term[0].isalnum() else ""
            right = r"\b" if term[-1].isalnum() else ""
            parts.append(left + esc + right)
        if parts:
            out.append((tag, re.compile("|".join(parts), re.I)))
    return out


def from_keywords(text: str) -> set[str]:
    """Deterministic tags. Works with no model configured."""
    hay = " " + " ".join(text.lower().split()) + " "
    return {tag for tag, pat in _rules() if pat.search(hay)}


def clean(candidates: list[str] | None) -> set[str]:
    """Keep only tags that exist in the vocabulary, normalised."""
    if not candidates:
        return set()
    vocab = vocabulary()
    return {t.strip().lower() for t in candidates if t and t.strip().lower() in vocab}


def apply(item: Item, body: str = "", model_tags: list[str] | None = None) -> None:
    """Merge model tags with keyword tags onto the item, capped and sorted."""
    text = f"{item.title} {item.one_liner} {item.summary} {body[:2000]}"
    merged = clean(model_tags) | from_keywords(text)

    # Source-derived tags the text cannot express on its own.
    if any(s.get("domain", "").endswith("github.com") for s in item.sources):
        merged.add("opensource")

    # Eight is roughly where a tag row stops being scannable on a card.
    item.tags = sorted(merged)[:8]
