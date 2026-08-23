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

    Terms with a trailing space in the config (" ai ", "rat ") are intentional
    word-boundary guards; they stay as written rather than being stripped.
    """
    out = []
    for tag, terms in _load("tags.yaml")["rules"].items():
        if tag not in vocabulary():
            continue
        pattern = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
        out.append((tag, re.compile(pattern, re.I)))
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
