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
    """Environment first, then .env — same precedence as the API key.

    Sanitised, because Telegram carries the token in the URL PATH, so a
    control character in it makes InvalidURL embed the whole path — token
    included — in an exception this module logs in three places.
    """
    from .config import clean_secret
    return clean_secret(os.environ.get(name) or _load_dotenv().get(name), name)


def _mask(chat_id: str) -> str:
    """Partially hide the destination in logs.

    Not a credential — it grants nothing without the bot token — but it is an
    identifier, and console output ends up in CI logs and screenshots.
    """
    if chat_id.startswith("@"):
        return chat_id[:3] + "***"
    return "***" + chat_id[-4:] if len(chat_id) > 4 else "***"


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



def _discover_chats(token: str) -> list[tuple[str, str]]:
    """Chat ids the bot can already reach, from getUpdates.

    Telegram only reveals a chat once someone has interacted with the bot
    there, which is why this returns nothing until you message it.
    """
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/getUpdates")
        with urllib.request.urlopen(req, timeout=15, context=_SSL) as resp:
            updates = json.load(resp).get("result", [])
    except Exception as exc:
        log("notify", f"! could not read updates: {exc}")
        return []

    seen: dict[str, str] = {}
    for upd in updates:
        msg = (upd.get("message") or upd.get("channel_post")
               or upd.get("my_chat_member") or {})
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is None:
            continue
        name = (chat.get("title") or chat.get("username")
                or chat.get("first_name") or "chat")
        seen[str(cid)] = f"{chat.get('type', '?')}: {name}"
    return sorted(seen.items())


def selftest() -> int:
    """Check the bot works BEFORE a run depends on it.

    Three setup mistakes look identical from the outside: a wrong token, a
    bot never added to the channel, and a bot added without admin rights.
    This tells them apart so you are not guessing.
    """
    cfg = settings().get("notify", {}).get("telegram", {})
    token = _secret(cfg.get("token_env", "TELEGRAM_BOT_TOKEN"))
    chat_id = _secret(cfg.get("chat_env", "TELEGRAM_CHAT_ID"))

    if not token:
        print("TELEGRAM_BOT_TOKEN is not set.")
        print("  @BotFather -> /newbot -> put the token in .env")
        return 1
    if not chat_id:
        # Finding your own chat id is the fiddliest part of Telegram setup,
        # so discover it rather than making the user hunt for it. Anyone who
        # has messaged the bot shows up in getUpdates.
        print("TELEGRAM_CHAT_ID is not set. Looking for recent chats...")
        found = _discover_chats(token)
        if found:
            print("")
            print("  Send one of these to Telegram? Add to .env:")
            for cid, label in found:
                print(f"    TELEGRAM_CHAT_ID={cid}      ({label})")
        else:
            print("")
            print("  No chats found. Do ONE of these, then re-run:")
            print("   a) Open your bot in Telegram and press Start / send 'hi'")
            print("      -> it will DM you the digest, no channel needed")
            print("   b) Create your own channel, add the bot as admin,")
            print("      post any message there, then re-run this")
        return 1

    # 1. Is the token itself valid?
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/getMe")
        with urllib.request.urlopen(req, timeout=15, context=_SSL) as resp:
            me = json.load(resp)["result"]
        print(f"token OK   -> bot is @{me.get('username')}")
    except urllib.error.HTTPError as exc:
        print(f"token REJECTED (HTTP {exc.code}). Re-copy it from @BotFather.")
        return 1
    except Exception as exc:
        print(f"could not reach Telegram: {exc}")
        return 1

    # 2. Can it actually post to the channel?
    ok = _send(token, chat_id,
               "<b>Newsdesk</b>\n"
               "Setup test - if you can read this, the bot is configured "
               "correctly.", 15)
    if ok:
        print(f"posting OK -> test message sent to {chat_id}")
        print("")
        print("Now set  notify.telegram.enabled: true  in "
              "config/settings.yaml to start sending digests.")
        return 0

    print(f"posting FAILED to {chat_id}.")
    print("  Most likely the bot is not an ADMIN of the channel.")
    print("  Channel -> Administrators -> Add Admin -> pick your bot,")
    print("  and make sure 'Post Messages' is enabled.")
    return 1

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
    log("notify", f"{sent}/{len(messages)} message(s) sent to {_mask(chat_id)} "
                  f"({len(live)} items)")
