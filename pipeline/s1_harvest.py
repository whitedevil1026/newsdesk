"""Stage 1 — harvest.

Pull every feed in the registry, in parallel, and return raw Articles
inside the lookback window. Network failures never abort the run; a dead
feed is logged and skipped.
"""

from __future__ import annotations

import ssl
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import feedparser

from .config import feeds, settings
from .models import Article
from .utils import domain_of, log, strip_html

# Windows Python ships no usable CA store, so several perfectly valid feeds
# (CISA, Anthropic, TechCrunch) fail with CERTIFICATE_VERIFY_FAILED. certifi
# supplies a current bundle. Verification stays ON — this fixes the trust
# store, it does not disable the check.
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()


def _parse_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return datetime(*st[:6], tzinfo=timezone.utc)
    return None


def _fetch_bytes(url: str, cfg: dict) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": cfg["harvest"]["user_agent"],
                 "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
                 "Cache-Control": "no-cache"},
    )
    with urllib.request.urlopen(req, timeout=cfg["harvest"]["timeout_seconds"],
                                context=_SSL_CTX) as resp:
        return resp.read()


def _fetch_one(category: str, spec: dict, cutoff: datetime, cfg: dict) -> list[Article]:
    parsed = feedparser.parse(_fetch_bytes(spec["url"], cfg))
    if getattr(parsed, "bozo", 0) and not parsed.entries:
        log("harvest", f"! {spec['name']}: {getattr(parsed, 'bozo_exception', 'no entries')}")
        return []

    out: list[Article] = []
    for entry in parsed.entries[: cfg["harvest"]["max_per_feed"]]:
        link = entry.get("link") or ""
        title = (entry.get("title") or "").strip()
        if not link or not title:
            continue
        published = _parse_date(entry)
        # Undated entries are kept — stage 4 re-checks the date from the page.
        if published and published < cutoff:
            continue
        out.append(
            Article(
                url=link,
                title=title,
                source=spec["name"],
                domain=domain_of(link),
                category=category,
                tier=spec.get("tier", "C"),
                published=published,
                summary_raw=strip_html(entry.get("summary") or "")[:1200],
                is_primary=bool(spec.get("primary", False)),
                is_aggregator=bool(spec.get("aggregator", False)),
            )
        )
    log("harvest", f"  {spec['name']:<24} {len(out):>3} in window")
    return out


def run() -> list[Article]:
    cfg = settings()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg["window"]["lookback_hours"])
    registry = feeds()

    jobs = [(cat, spec) for cat, specs in registry.items() for spec in specs]
    log("harvest", f"{len(jobs)} feeds, window = {cfg['window']['lookback_hours']}h")

    articles: list[Article] = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(_fetch_one, c, s, cutoff, cfg): s for c, s in jobs}
        for fut in as_completed(futures):
            try:
                articles.extend(fut.result())
            except Exception as exc:                      # a feed dying is normal
                log("harvest", f"! {futures[fut]['name']}: {exc}")

    log("harvest", f"total {len(articles)} articles")
    return articles
