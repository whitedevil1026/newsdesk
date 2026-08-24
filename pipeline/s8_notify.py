"""Stage 8 — push the digest to Telegram.

Optional, and OFF by default. It needs a bot token you create yourself, and
it posts to a channel other people may read, so it never runs on a config
default — both `notify.telegram.enabled` and a token must be present.

Deliberately narrow in what it sends. The website is the full record; this
is the "worth interrupting you for" subset. Sending forty cards to a channel
every morning trains people to ignore it, which costs more than sending
nothing.

Setup:
  1. Talk to @BotFather on Telegram, /newbot, copy the token
  2. Add the bot to your channel as an ADMIN (it cannot post otherwise)
  3. Put these in .env:
         TELEGRAM_BOT_TOKEN=123456:ABC...
         TELEGRAM_CHAT_ID=@yourchannel
  4. Set notify.telegram.enabled: true in config/settings.yaml
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

from .config import _load_dotenv, settings
from .models import Item, Priority, Verdict
from .utils import log, truncate

try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL = ssl.create_default_context()

API = "https://api.telegram.org/bot{token}/sendMessage"

# Telegram hard-limits a message to 4096 characters. Leave room so a long
# title cannot push a message over the edge mid-send.
MAX_CHARS = 3800

_BADGE = {Priority.CRITICAL: "\U0001F534", Priority.IMPORTANT: "\U0001F7E0",
          Priority.MINOR: "⚪"}
_MARK = {Verdict.VERIFIED: "verified", Verdict.CORROBORATED: "corroborated",
         Verdict.ESTABLISHED: "established", Verdict.UNVERIFIED: "single source",
         Verdict.DISPUTED: "disputed"}


def _secret(name: str) -> str | None:
    """Environment first, then .env — same precedence as the API key."""
    return os.environ.get(name) or _load_dotenv().get(name) or None


def _esc(text: str) -> str:
    """Escape for Telegram's HTML parse mode."""
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _format(items: list[Item], generated: str) -> list[str]:
    """Build one or more messages, split on Telegram's length limit."""
    header = f"<b>Newsdesk</b> — {generated[:16].replace('T', ' ')} UTC\n"
    chunks: list[str] = []
    current = header

    for item in items:
        badge = _BADGE.get(item.priority, "⚪")
        mark = _MARK.get(item.verdict, "")
        url = item.sources[0]["url"] if item.sources else ""
        outlets = (f" · {item.corroboration} outlets"
                   if item.corroboration > 1 else "")

        block = (f"\n{badge} <b>{_esc(truncate(item.title, 130))}</b>\n"
                 f"<i>{_esc(mark)}{outlets}</i>\n")
        if item.bottom_line:
            block += f"{_esc(truncate(item.bottom_line, 260))}\n"
        if url:
            block += f'<a href="{_esc(url)}">read</a>\n'

        if len(current) + len(block) > MAX_CHARS:
            chunks.append(current)
            current = header + block
        else:
            current += block

    if current.strip() != header.strip():
        chunks.append(current)
    return chunks


def _send(token: str, chat_id: str, text: str, timeout: int) -> bool:
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(API.format(token=token), data=data)
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
            return json.load(resp).get("ok", False)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:200]
        # 403 almost always means the bot is not an admin of the channel.
        log("notify", f"! HTTP {exc.code}: {body}")
    except Exception as exc:
        log("notify", f"! {exc}")
    return False


def run(items: list[Item], generated_at: str, dry_run: bool = False) -> None:
    cfg = settings().get("notify", {}).get("telegram", {})
    if not cfg.get("enabled", False):
        return

    token = _secret(cfg.get("token_env", "TELEGRAM_BOT_TOKEN"))
    chat_id = _secret(cfg.get("chat_env", "TELEGRAM_CHAT_ID"))
    if not (token and chat_id):
        log("notify", "! telegram enabled but TELEGRAM_BOT_TOKEN / "
                      "TELEGRAM_CHAT_ID missing — skipped")
        return

    # Only what is worth interrupting someone for.
    floor = cfg.get("min_priority", "important")
    wanted = {Priority.CRITICAL} if floor == "critical" else \
        {Priority.CRITICAL, Priority.IMPORTANT}
    live = [i for i in items
            if i.verdict is not Verdict.REJECTED and i.priority in wanted]
    live = live[: cfg.get("max_items", 10)]

    if not live:
        log("notify", f"nothing at or above '{floor}' — nothing sent")
        return

    messages = _format(live, generated_at)
    if dry_run:
        log("notify", f"DRY RUN — would send {len(messages)} message(s), "
                      f"{len(live)} items")
        print("\n".join(messages)[:1500])
        return

    sent = sum(_send(token, chat_id, m, 20) for m in messages)
    log("notify", f"{sent}/{len(messages)} message(s) sent to {chat_id} "
                  f"({len(live)} items)")
