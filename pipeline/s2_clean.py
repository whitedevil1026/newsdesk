"""Stage 2 — clean and cluster.

Three jobs: drop junk, drop things already published in an earlier run,
and group articles that describe the same story so corroboration can be
counted in stage 4.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import CACHE_DIR, interests, settings
from . import netguard
from .models import Article, Cluster, canonical_url
from .utils import domain_of, log, overlap, tokens

SEEN_PATH = CACHE_DIR / "seen.json"
BLOCKLIST_PATH = CACHE_DIR / "iffy_blocklist.txt"


def _load_seen() -> set[str]:
    if SEEN_PATH.exists():
        return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
    return set()


def save_seen(uids: set[str]) -> None:
    """Called by stage 7 only — an item is 'seen' once it is published."""
    merged = sorted(_load_seen() | uids)[-20000:]     # bounded history
    SEEN_PATH.write_text(json.dumps(merged), encoding="utf-8")


def _blocklist() -> set[str]:
    """Iffy.news low-credibility domains. Absent file = empty blocklist."""
    if not BLOCKLIST_PATH.exists():
        log("clean", "no blocklist cached (run: python run.py --update-blocklist)")
        return set()
    return {
        line.strip().lower()
        for line in BLOCKLIST_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def _is_muted(article: Article, mutes: list[str]) -> bool:
    hay = f"{article.title} {article.summary_raw}".lower()
    return any(m.lower() in hay for m in mutes)


def run(articles: list[Article]) -> list[Cluster]:
    cfg = settings()
    mutes = interests().get("mute", [])
    blocked = _blocklist()
    seen = _load_seen()
    suppress = cfg["dedupe"]["mode"] == "suppress"
    if not suppress:
        log("clean", f"dedupe=reuse_summary — {len(seen)} known urls stay visible")

    kept: list[Article] = []
    by_uid: dict[str, Article] = {}
    dropped = {"blocked": 0, "muted": 0, "seen": 0, "dupe": 0, "short": 0,
               "unsafe_url": 0}

    for art in articles:
        # Scheme check only — a DNS lookup per article would add hundreds of
        # round trips. The extractor does the full check before fetching.
        if not netguard.is_safe(art.url, resolve_dns=False):
            dropped["unsafe_url"] += 1
            continue
        if art.domain in blocked:
            dropped["blocked"] += 1
            continue
        if _is_muted(art, mutes):
            dropped["muted"] += 1
            continue
        if suppress and art.uid in seen:
            dropped["seen"] += 1
            continue
        if len(tokens(art.title)) < cfg["cluster"]["min_title_tokens"]:
            dropped["short"] += 1
            continue
        if art.uid in by_uid:                 # exact same link from two feeds
            dropped["dupe"] += 1
            continue
        by_uid[art.uid] = art
        kept.append(art)

    log("clean", f"kept {len(kept)}  dropped {dict(dropped)}")

    clusters = _cluster(
        kept,
        cfg["cluster"]["title_similarity"],
        cfg["cluster"]["min_shared_tokens"],
    )
    multi = sum(1 for c in clusters if len(c.articles) > 1)
    log("clean", f"{len(clusters)} clusters ({multi} multi-outlet)")
    return clusters


def _cluster(articles: list[Article], threshold: float,
             min_shared: int) -> list[Cluster]:
    """Greedy single-pass clustering on headline token overlap.

    Two gates, both required: a high overlap coefficient AND an absolute
    floor of shared content words. The floor matters because overlap
    alone lets two very short headlines match on one common word.

    Thresholds were calibrated against a live 363-article harvest, where
    0.55 / 3 recovered every genuine cross-outlet pair with no false
    positives. Good enough at this volume and needs no model; swap for
    embeddings if paraphrased headlines start slipping through.
    """
    clusters: list[Cluster] = []
    sigs: list[set[str]] = []

    # Longest headlines first: they make better cluster seeds than stubs.
    for art in sorted(articles, key=lambda a: -len(a.title)):
        sig = tokens(art.title)
        placed = False
        for idx, existing in enumerate(sigs):
            if len(sig & existing) >= min_shared and overlap(sig, existing) >= threshold:
                clusters[idx].articles.append(art)
                sigs[idx] = existing | sig
                placed = True
                break
        if not placed:
            clusters.append(Cluster(key=art.uid, articles=[art]))
            sigs.append(sig)

    return clusters
