"""Stage 1c — posts from Bluesky.

This exists because X/Twitter is no longer viable: the free tier was
discontinued for new developers in February 2026 and reads are now billed
per post. Much of the infosec and AI community that this project follows has
an active Bluesky presence, and Bluesky's public read API needs no
authentication, no key, and no account.

Posts are noisier than articles, so two filters run before anything is kept:
replies and reposts are dropped (they are conversation, not signal), and a
post must clear a small engagement floor or contain a link.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .config import settings
from .models import Article
from .utils import headline, log, truncate

try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL = ssl.create_default_context()

API = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"


def _fetch(handle: str, limit: int, cfg: dict) -> list[dict]:
    url = f"{API}?{urllib.parse.urlencode({'actor': handle, 'limit': limit, 'filter': 'posts_no_replies'})}"
    req = urllib.request.Request(url, headers={"User-Agent": cfg["harvest"]["user_agent"]})
    try:
        with urllib.request.urlopen(req, timeout=cfg["harvest"]["timeout_seconds"],
                                    context=_SSL) as resp:
            return json.load(resp).get("feed", [])
    except urllib.error.HTTPError as exc:
        # 400 means the handle does not resolve — a config typo, worth saying.
        log("bluesky", f"! {handle}: HTTP {exc.code}"
                       f"{' (handle not found?)' if exc.code == 400 else ''}")
        return []
    except Exception as exc:
        log("bluesky", f"! {handle}: {exc}")
        return []


def _post_url(post: dict, handle: str) -> str:
    # at://did:plc:xxx/app.bsky.feed.post/RKEY -> the web permalink
    rkey = post.get("uri", "").rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{handle}/post/{rkey}"


def _external_link(post: dict) -> str | None:
    embed = post.get("record", {}).get("embed", {}) or {}
    ext = embed.get("external") or {}
    return ext.get("uri")


def run() -> list[Article]:
    cfg = settings()
    bs = cfg.get("bluesky")
    if not bs or not bs.get("enabled", False):
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=bs["lookback_hours"])
    out: list[Article] = []

    for spec in bs["accounts"]:
        handle = spec["handle"]
        kept = 0
        for entry in _fetch(handle, bs["per_account"], cfg):
            post = entry.get("post", {})

            # A repost is someone else's content surfacing again; the original
            # will be picked up on its own if it matters.
            if entry.get("reason"):
                continue

            record = post.get("record", {})
            text = (record.get("text") or "").strip()
            if len(text) < bs["min_chars"]:
                continue

            created = record.get("createdAt")
            try:
                when = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except (AttributeError, ValueError):
                continue
            if when < cutoff:
                continue

            likes = post.get("likeCount", 0)
            reposts = post.get("repostCount", 0)
            link = _external_link(record)

            # Either the community reacted, or the author is pointing somewhere.
            if likes < bs["min_likes"] and not link:
                continue

            body = text
            if link:
                body += f"\n\nLinked: {link}"
            body += f"\n\n(Bluesky post by @{handle} — {likes} likes, {reposts} reposts)"

            out.append(Article(
                url=_post_url(post, handle),
                title=headline(text.replace("\n", " "), 150),
                source=spec.get("name", handle),
                domain="bsky.app",
                category=spec["category"],
                tier="C",           # a post is one person's word, never tier A
                published=when,
                summary_raw=body,
                is_primary=False,
            ))
            kept += 1

        if kept:
            log("bluesky", f"  {handle:<32} {kept:>2} posts")

    log("bluesky", f"{len(out)} posts from {len(bs['accounts'])} accounts")
    return out
