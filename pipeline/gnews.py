"""Resolve Google News redirect tokens to real publisher URLs.

Google News RSS hands back links like

    news.google.com/rss/articles/CBMifkFVX3lxTE41Y2tXZGJaUUozS0Jx...

which are useless as reference links: the reader sees a Google URL instead of
the BBC or Reuters article, and the domain cannot be used for source tiering
or de-duplication either.

Older tokens had the destination base64'd inside them. Current ones
(`AU_yq...`) do not — decoding them offline yields nothing. What does work is
the endpoint Google's own web UI calls: fetch the article page for a
signature and timestamp, then POST both to `batchexecute`, which returns the
publisher URL.

Two requests per link, so this is deliberately NOT run over the whole
harvest. Only links that are actually going to be published get resolved, and
every result is cached permanently — a token's destination never changes.
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import netguard
from .config import CACHE_DIR, settings
from .utils import log

try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL = ssl.create_default_context()

CACHE_PATH = CACHE_DIR / "gnews_urls.json"
BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"

_SIG = re.compile(r'data-n-a-sg="([^"]+)"')
_TS = re.compile(r'data-n-a-ts="([^"]+)"')
_REAL = re.compile(r'https?://(?!news\.google)[^\\"]+')


# Google News titles carry the publisher as a trailing " - Publisher".
_TITLE_SUFFIX = re.compile(r"\s+-\s+([^-]{2,40})$")

# Publisher name -> the domain we would have got by resolving. Only needs to
# cover outlets we tier or de-duplicate on; anything else falls back to a
# slug, which is still unique per publisher and good enough for counting
# independent outlets.
_KNOWN = {
    "reuters": "reuters.com", "the hindu": "thehindu.com",
    "bbc": "bbc.co.uk", "bbc news": "bbc.co.uk",
    "the verge": "theverge.com", "techcrunch": "techcrunch.com",
    "ars technica": "arstechnica.com", "bleepingcomputer": "bleepingcomputer.com",
    "the hacker news": "thehackernews.com", "wired": "wired.com",
    "cnbc": "cnbc.com", "bloomberg": "bloomberg.com",
    "the economic times": "economictimes.indiatimes.com",
    "ndtv": "ndtv.com", "livemint": "livemint.com", "mint": "livemint.com",
    "the indian express": "indianexpress.com", "hindustan times": "hindustantimes.com",
    "dark reading": "darkreading.com", "securityweek": "securityweek.com",
    "infosecurity magazine": "infosecurity-magazine.com",
    "help net security": "helpnetsecurity.com", "the register": "theregister.com",
    "zdnet": "zdnet.com", "cyberscoop": "cyberscoop.com",
    "krebs on security": "krebsonsecurity.com", "404 media": "404media.co",
    "venturebeat": "venturebeat.com", "engadget": "engadget.com",
}


def publisher_of(title: str) -> tuple[str, str] | None:
    """(display name, domain) from a Google News title, without a network call.

    Resolving every link would be two HTTP requests each — hundreds per run.
    The title already names the publisher, and that is all the pipeline needs
    for source tiering and independent-outlet counting. The real URL is only
    needed for links that actually reach the page, so it is fetched later and
    only for those.
    """
    match = _TITLE_SUFFIX.search(title)
    if not match:
        return None
    name = match.group(1).strip()
    key = name.lower()
    domain = _KNOWN.get(key)
    if not domain:
        # A stable pseudo-domain: unique per publisher, obviously synthetic,
        # and never mistaken for a real host.
        slug = re.sub(r"[^a-z0-9]+", "-", key).strip("-")
        if not slug:
            return None
        domain = f"{slug}.publisher"
    return name, domain


def strip_publisher(title: str) -> str:
    return _TITLE_SUFFIX.sub("", title).strip()


def is_gnews(url: str) -> bool:
    return "news.google.com" in url and "/articles/" in url


def _load_cache() -> dict[str, str]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("gnews", "! cache corrupt, starting fresh")
    return {}


def _save_cache(cache: dict[str, str]) -> None:
    CACHE_PATH.write_text(json.dumps(cache, indent=0), encoding="utf-8")


def _get(url: str, ua: str, timeout: int) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
        return resp.read().decode("utf-8", "replace")


def _resolve_one(url: str, ua: str, timeout: int) -> str | None:
    """Two-step resolution. Returns None on any failure — never raises.

    A failure here is not important enough to disturb the run: the caller
    keeps the original Google link, which still works for a human clicking it.
    """
    try:
        article_id = url.split("/articles/")[1].split("?")[0]
        html = _get(url, ua, timeout)
        sig, ts = _SIG.search(html), _TS.search(html)
        if not (sig and ts):
            return None

        inner = json.dumps([
            "garturlreq",
            [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
              None, None, None, None, None, 0, 1],
             "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
            article_id, int(ts.group(1)), sig.group(1),
        ])
        payload = json.dumps([[["Fbv4je", inner, None, "generic"]]])
        data = urllib.parse.urlencode({"f.req": payload}).encode()

        req = urllib.request.Request(
            BATCH_URL, data=data,
            headers={"User-Agent": ua,
                     "Content-Type":
                         "application/x-www-form-urlencoded;charset=UTF-8"})
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
            body = resp.read().decode("utf-8", "replace")

        match = _REAL.search(body)
        if not match:
            return None
        # The destination is chosen by whoever published to Google News, so
        # it is untrusted even though the endpoint is Google's.
        target = match.group(0)
        return target if netguard.is_safe(target, resolve_dns=False) else None
    except Exception:
        return None


def resolve_many(urls: list[str]) -> dict[str, str]:
    """Resolve a batch, using and updating the cache. Best effort."""
    cfg = settings()["harvest"]
    ua, timeout = cfg["user_agent"], cfg["timeout_seconds"]

    cache = _load_cache()
    todo = [u for u in dict.fromkeys(urls) if is_gnews(u) and u not in cache]
    if not todo:
        return cache

    log("gnews", f"resolving {len(todo)} Google News links "
                 f"({len(cache)} already cached)")

    ok = 0
    # Modest concurrency: this is Google's own front-end endpoint and the
    # volume is small. Hammering it would be both rude and self-defeating.
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_resolve_one, u, ua, timeout): u for u in todo}
        for fut in as_completed(futures):
            real = fut.result()
            if real:
                cache[futures[fut]] = real
                ok += 1

    _save_cache(cache)
    log("gnews", f"{ok}/{len(todo)} resolved to publisher URLs")
    return cache
