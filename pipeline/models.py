"""Data types that flow between stages.

Every stage takes a list of these and returns a list of these, so any
stage can be run, dumped to JSON, inspected, and replayed on its own.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    """How well established a story is.

    Rule-based on the signals themselves rather than a numeric threshold.
    The old scheme leaned on a trust score crossing 65, which was unreachable
    for this feed mix and left 96% of items on one label — technically true,
    informationally useless.

    Corroboration and source quality are different questions and are now kept
    apart: a BleepingComputer exclusive and an anonymous GitHub repo are both
    "one outlet", but they are not the same claim on your attention.
    """

    VERIFIED = "verified"          # primary artifact, or 3+ independent outlets
    CORROBORATED = "corroborated"  # 2 independent outlets agree
    ESTABLISHED = "established"    # one outlet, but a known publication (tier A/B)
    UNVERIFIED = "unverified"      # one outlet, blog/aggregator/repo (tier C)
    DISPUTED = "disputed"          # outlets or the judge disagree on a key fact
    REJECTED = "rejected"          # never published; see Item.reject_reason


class Priority(str, Enum):
    CRITICAL = "critical"
    IMPORTANT = "important"
    MINOR = "minor"


@dataclass
class Article:
    """One entry as harvested from one feed. Raw, pre-clustering."""

    url: str
    title: str
    source: str                 # display name, e.g. "BleepingComputer"
    domain: str
    category: str               # cybersecurity | tech_ai | markets | india_world
    tier: str                   # A | B | C
    published: datetime | None
    summary_raw: str = ""       # feed blurb, before extraction
    body: str = ""              # full text, filled by stage 3
    is_primary: bool = False    # feed is an official/primary source
    is_aggregator: bool = False # link is a redirect stub, not the article
    window_hours: int = 0       # per-feed override it was harvested under

    @property
    def uid(self) -> str:
        """Stable id for dedupe across runs — canonical url, not title."""
        return hashlib.sha1(canonical_url(self.url).encode()).hexdigest()[:16]


@dataclass
class Cluster:
    """A group of Articles that all cover the same underlying story."""

    key: str
    articles: list[Article] = field(default_factory=list)

    @property
    def lead(self) -> Article:
        """Best article to represent the cluster.

        Aggregator entries sort last regardless of tier: their links are
        redirect stubs, and the lead article's URL is what the reader clicks.
        """
        return sorted(
            self.articles,
            key=lambda a: (a.is_aggregator, not a.is_primary, a.tier, -len(a.body)),
        )[0]

    @property
    def independent_outlets(self) -> int:
        """Distinct domains. Syndication collapsing happens in stage 4."""
        return len({a.domain for a in self.articles})

    @property
    def has_primary(self) -> bool:
        return any(a.is_primary for a in self.articles)


@dataclass
class Claim:
    """One atomic assertion pulled out of a generated summary."""

    text: str
    support_span: str = ""       # sentence from the body it was drawn from
    status: str = "unchecked"    # unchecked | entailed | neutral | contradicted


@dataclass
class Item:
    """A publishable card. What stages 5-7 build and the website renders."""

    cluster_key: str
    title: str
    category: str
    one_liner: str = ""          # the single "why this matters" sentence
    tags: list[str] = field(default_factory=list)   # controlled vocabulary
    summary: str = ""            # the full idea, 3-5 sentences
    key_facts: list[str] = field(default_factory=list)  # the numbers that matter
    bottom_line: str = ""        # the single line that changes what you do
    claims: list[Claim] = field(default_factory=list)

    # scoring
    trust_score: int = 0
    interest_score: int = 0
    corroboration: int = 1
    blend: float = 0.0
    is_new: bool = False         # not on the page in any previous run
    # "model" once a model has written the summary, "extract" while it is
    # still the first few sentences lifted from the article. With the page
    # publishing everything in the window, most cards are extractive and the
    # reader has to be able to tell which is which.
    summary_source: str = "extract"
    alt_keys: list[str] = field(default_factory=list)  # every member article uid
    critical_signal: str = ""
    priority: Priority = Priority.MINOR
    verdict: Verdict = Verdict.UNVERIFIED

    # provenance — always carried to the card, never dropped
    sources: list[dict[str, str]] = field(default_factory=list)
    primary_links: list[str] = field(default_factory=list)
    published: datetime | None = None

    # audit trail: why this passed or failed, shown behind the card's toggle
    trace: list[str] = field(default_factory=list)
    reject_reason: str = ""

    def note(self, msg: str) -> None:
        self.trace.append(msg)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["priority"] = self.priority.value
        d["verdict"] = self.verdict.value
        d["published"] = self.published.isoformat() if self.published else None
        return d


def canonical_url(url: str) -> str:
    """Strip tracking params and fragments so the same story dedupes cleanly."""
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

    parts = urlsplit(url)
    junk = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
            "fbclid", "gclid", "ref", "source", "amp", "at_medium", "at_campaign"}
    q = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in junk]
    host = parts.netloc.lower().removeprefix("www.")
    return urlunsplit((parts.scheme, host, parts.path.rstrip("/"), urlencode(q), ""))
