"""Stage 1 — harvest.

Pull every feed in the registry, in parallel, and return raw Articles
inside the lookback window. Network failures never abort the run; a dead
feed is logged and skipped.
"""

from __future__ import annotations

import html
import ssl
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import feedparser

from . import gnews
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


class _Redirect308(urllib.request.HTTPRedirectHandler):
    """Follow HTTP 308 Permanent Redirect.

    urllib handles 301/302/303/307 but not 308, so a feed that moved with a
    308 raised HTTPError and was recorded as dead. Two feeds hit this
    (Intigriti, ProjectDiscovery) and both are perfectly alive — the redirect
    just was not being followed.
    """

    def http_error_308(self, req, fp, code, msg, headers):
        return self.http_error_301(req, fp, 301, msg, headers)


_OPENER = urllib.request.build_opener(_Redirect308)


def _fetch_bytes(url: str, cfg: dict) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": cfg["harvest"]["user_agent"],
                 "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
                 "Cache-Control": "no-cache"},
    )
    # _OPENER, not urlopen: it carries the 308 handler above.
    import ssl as _ssl
    https = urllib.request.HTTPSHandler(context=_SSL_CTX)
    opener = urllib.request.build_opener(_Redirect308, https)
    with opener.open(req, timeout=cfg["harvest"]["timeout_seconds"]) as resp:
        return resp.read()


def window_for(category: str, cfg: dict, spec: dict | None = None) -> int:
    """Lookback for one feed.

    Three levels, most specific first: a per-feed `window_hours`, then the
    category window, then the global default.

    The per-feed level exists because publishing cadence is a property of the
    SOURCE, not its category. Deep research labs — watchTowr, Assetnote,
    Doyensec, Include Security, Sonar — publish once or twice a month, and
    every one of them returned zero items under a 168h category window even
    though the feeds were verified working. A month-old teardown of a
    pre-auth RCE has not stopped being worth reading.
    """
    if spec and spec.get("window_hours"):
        return int(spec["window_hours"])
    per = cfg["window"].get("per_category_hours", {})
    return per.get(category, cfg["window"]["lookback_hours"])


def _fetch_one(category: str, spec: dict, cutoff: datetime, cfg: dict) -> list[Article]:
    parsed = feedparser.parse(_fetch_bytes(spec["url"], cfg))
    if getattr(parsed, "bozo", 0) and not parsed.entries:
        log("harvest", f"! {spec['name']}: {getattr(parsed, 'bozo_exception', 'no entries')}")
        return []

    out: list[Article] = []
    for entry in parsed.entries[: cfg["harvest"]["max_per_feed"]]:
        link = entry.get("link") or ""
        # Feeds escape their titles, and nothing downstream decoded them, so
        # cards rendered "Brazilian &amp; Global Financial Systems" and
        # "GoPro says it&#8217;s still committed". unescape twice: a few feeds
        # double-encode, which leaves "&amp;amp;" after one pass.
        title = html.unescape(html.unescape(
            (entry.get("title") or "").strip())).strip()
        if not link or not title:
            continue
        # Google News wraps every story in a redirect and names the real
        # publisher in the title. Attribute it here, for free, so clustering
        # and outlet-counting see the true source instead of "news.google.com".
        src_name, src_domain, aggregator = spec["name"], domain_of(link),             bool(spec.get("aggregator", False))
        if gnews.is_gnews(link):
            found = gnews.publisher_of(title)
            if found:
                src_name, src_domain = found
                title = gnews.strip_publisher(title)

        published = _parse_date(entry)
        # Undated entries are kept — stage 4 re-checks the date from the page.
        if published and published < cutoff:
            continue
        out.append(
            Article(
                url=link,
                title=title,
                source=src_name,
                domain=src_domain,
                category=category,
                tier=spec.get("tier", "C"),
                published=published,
                summary_raw=strip_html(entry.get("summary") or "")[:1200],
                is_primary=bool(spec.get("primary", False)),
                is_aggregator=aggregator,
                window_hours=int(spec.get("window_hours") or 0),
            )
        )
    log("harvest", f"  {spec['name']:<24} {len(out):>3} in window")
    return out


def run() -> list[Article]:
    cfg = settings()
    now = datetime.now(timezone.utc)
    registry = feeds()

    jobs = [(cat, spec) for cat, specs in registry.items() for spec in specs]
    windows = {cat: window_for(cat, cfg) for cat in registry}
    log("harvest", f"{len(jobs)} feeds, windows " +
        ", ".join(f"{c}={h}h" for c, h in windows.items()))

    articles: list[Article] = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {
            pool.submit(_fetch_one, c, s,
                        now - timedelta(hours=window_for(c, cfg, s)), cfg): s
            for c, s in jobs
        }
        for fut in as_completed(futures):
            try:
                articles.extend(fut.result())
            except Exception as exc:                      # a feed dying is normal
                log("harvest", f"! {futures[fut]['name']}: {exc}")

    log("harvest", f"total {len(articles)} articles")
    return articles
