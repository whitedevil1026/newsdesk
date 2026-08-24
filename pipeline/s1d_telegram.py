"""Stage 1d — public Telegram channels.

Telegram exposes a read-only web preview of any PUBLIC channel at
``t.me/s/<name>``. No bot, no token, no API key, no account — verified
against several channels before this was written. Private channels have no
such preview and would need a bot added to them; that is not supported here.

Channels are a genuinely different source class from RSS. A CTI channel
posts a link plus a line of context within minutes of something breaking,
often well before anyone writes an article about it. What they do not give
is article text, so most items arrive as a headline and a URL — stage 3
fetches the real body from the link.

Many channels post through link shorteners (ift.tt, bit.ly, buff.ly).
Those are resolved here, because an unresolved shortener breaks three things
at once: source tiering, de-duplication, and the reference link a reader
clicks.
"""

from __future__ import annotations

import html
import re
import ssl
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from . import netguard
from .config import settings
from .models import Article
from .utils import log, truncate

try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL = ssl.create_default_context()

PREVIEW = "https://t.me/s/{channel}"

# One regex per MESSAGE, not one for text and another for time.
#
# The previous version matched text divs and <time> tags independently and
# zipped them with `stamps[-len(blocks):]`. That is only correct when every
# extra timestamp precedes the first text block, which is not how a channel
# looks: a media-only post (routine on vxunderground) produces a stamp with
# no text block, and a REPLY produces two text blocks for one stamp because
# the reply-preview div also starts with `tgme_widget_message_text`. Either
# way every subsequent post was paired with the wrong time — silently, since
# when the counts happen to match the slice is a no-op.
_MESSAGE = re.compile(
    r'<div class="tgme_widget_message[ "].*?(?=<div class="tgme_widget_message[ "]|\Z)',
    re.S)
_TEXT_IN_MSG = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_TIME_IN_MSG = re.compile(r'<time datetime="([^"]+)"')
_HREF = re.compile(r'href="(https?://[^"]+)"')
_TAG = re.compile(r"<[^>]+>")
_BR = re.compile(r"<br\s*/?>", re.I)

# Hosts that only ever redirect. Their domain says nothing about the source.
SHORTENERS = {"ift.tt", "bit.ly", "buff.ly", "t.co", "dlvr.it", "ow.ly",
              "tinyurl.com", "lnkd.in", "trib.al", "shorturl.at", "rb.gy"}



def _known_sources() -> dict[str, tuple[str, str]]:
    """domain -> (display name, tier) for every outlet we already harvest.

    A channel post is a POINTER, not a source. When ctinow links to
    bleepingcomputer.com, the article is BleepingComputer's — tiering it C
    because a Telegram channel happened to surface it understates it badly,
    and on a live run left genuine security reporting sitting below the
    publication bar.
    """
    from .config import feeds
    from .utils import domain_of

    out: dict[str, tuple[str, str]] = {}
    for specs in feeds().values():
        for spec in specs:
            if spec.get("aggregator"):
                continue
            dom = domain_of(spec["url"])
            if dom:
                out[dom] = (spec["name"], spec.get("tier", "C"))
    # Outlets that show up through channels but are not feeds of ours.
    out.setdefault("securityweek.com", ("SecurityWeek", "B"))
    out.setdefault("therecord.media", ("The Record", "B"))
    out.setdefault("infosecurity-magazine.com", ("Infosecurity", "B"))
    out.setdefault("helpnetsecurity.com", ("Help Net Security", "B"))
    out.setdefault("securityaffairs.com", ("Security Affairs", "B"))
    return out


def _fetch(channel: str, ua: str, timeout: int) -> str | None:
    try:
        req = urllib.request.Request(PREVIEW.format(channel=channel),
                                     headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # 404 means no public preview: the channel is private, renamed, or
        # does not exist. Worth saying plainly rather than failing silently.
        log("telegram", f"! {channel}: HTTP {exc.code}"
                        f"{' (private or missing?)' if exc.code == 404 else ''}")
    except Exception as exc:
        log("telegram", f"! {channel}: {exc}")
    return None


def _resolve_short(url: str, ua: str, timeout: int) -> str:
    """Follow a shortener to its destination. Returns the input on failure."""
    host = urllib.request.urlparse(url).hostname or "" \
        if hasattr(urllib.request, "urlparse") else ""
    if not host:
        from urllib.parse import urlsplit
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    if host not in SHORTENERS:
        return url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua},
                                     method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
            final = resp.geturl()
        return final if netguard.is_safe(final, resolve_dns=False) else url
    except Exception:
        return url


def _parse(block: str) -> tuple[str, str | None]:
    """(plain text, first external link) from one message's HTML."""
    text = html.unescape(_TAG.sub("", _BR.sub("\n", block))).strip()
    links = [u for u in _HREF.findall(block) if "t.me" not in u]
    return " ".join(text.split()), (links[0] if links else None)


def _channel_articles(spec: dict, cutoff: datetime, cfg: dict) -> list[Article]:
    ua = cfg["harvest"]["user_agent"]
    timeout = cfg["harvest"]["timeout_seconds"]
    channel = spec["channel"]

    page = _fetch(channel, ua, timeout)
    if not page:
        return []

    tg = cfg["telegram"]
    out: list[Article] = []

    # Each message carries its own timestamp, so they cannot drift apart.
    for chunk in _MESSAGE.findall(page):
        texts = _TEXT_IN_MSG.findall(chunk)
        stamps = _TIME_IN_MSG.findall(chunk)
        if not texts or not stamps:
            continue                      # media-only post, or no timestamp

        # The LAST text div is the message's own; an earlier one is the
        # quoted post in a reply. The LAST <time> is the post time; an
        # earlier one belongs to the quoted post.
        block, stamp = texts[-1], stamps[-1]
        text, link = _parse(block)
        if len(text) < tg["min_chars"]:
            continue
        try:
            when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when < cutoff:
            continue

        # A post with no link is commentary. Useful to a human scrolling the
        # channel, but there is nothing for the pipeline to verify against.
        if not link and tg.get("require_link", True):
            continue

        url = _resolve_short(link, ua, timeout) if link else \
            f"https://t.me/{channel}"
        if not netguard.is_safe(url, resolve_dns=False):
            continue

        # Strip the trailing URL from the title — it is already the link.
        title = re.sub(r"https?://\S+$", "", text).strip() or text
        from .utils import domain_of

        dom = domain_of(url)
        # Credit the outlet the link actually points at, not the channel that
        # surfaced it. A post pointing at BleepingComputer IS BleepingComputer.
        known = _known_sources().get(dom)
        if known:
            src_name, src_tier = known
        else:
            src_name = spec.get("name", channel)
            src_tier = spec.get("tier", "C")   # unknown host: one poster's word

        out.append(Article(
            url=url,
            title=truncate(title, 160),
            source=src_name,
            domain=dom,
            category=spec["category"],
            tier=src_tier,
            published=when,
            summary_raw=text,
            is_primary=False,
            window_hours=int(spec.get("window_hours") or 0),
        ))

    if out:
        log("telegram", f"  {channel:<24} {len(out):>2} posts")
    return out


def run() -> list[Article]:
    cfg = settings()
    tg = cfg.get("telegram")
    if not tg or not tg.get("enabled", False):
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=tg["lookback_hours"])
    channels = tg.get("channels", [])

    out: list[Article] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_channel_articles, c, cutoff, cfg)
                   for c in channels]
        for fut in as_completed(futures):
            try:
                out.extend(fut.result())
            except Exception as exc:
                log("telegram", f"! channel failed: {exc}")

    log("telegram", f"{len(out)} posts from {len(channels)} channel(s)")
    return out
