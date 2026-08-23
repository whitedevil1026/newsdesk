"""Small shared helpers: logging, text normalisation, JSON snapshots."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_SCRIPT_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")

_STOP = {"the", "a", "an", "of", "in", "on", "to", "for", "and", "is", "are",
         "at", "by", "with", "as", "from", "after", "over", "new", "says"}


def log(stage: str, msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"  [{ts}] {stage:<12} {msg}", file=sys.stderr)


def banner(text: str) -> None:
    print(f"\n{'=' * 68}\n  {text}\n{'=' * 68}", file=sys.stderr)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def tokens(title: str) -> set[str]:
    """Content words from a headline, for similarity comparison."""
    words = re.findall(r"[a-z0-9']+", title.lower())
    return {w for w in words if w not in _STOP and len(w) > 2}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def overlap(a: set[str], b: set[str]) -> float:
    """Overlap coefficient: shared tokens over the SHORTER headline.

    Preferred over Jaccard for cross-outlet matching. Two outlets covering
    one story routinely write headlines of very different length, and
    Jaccard penalises that asymmetry hard enough to miss real matches
    (measured on live data: true pairs scored 0.33-0.60 on Jaccard but
    0.55-0.86 here).
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def strip_html(text: str) -> str:
    """Feed blurbs routinely carry markup. It must never reach a summary."""
    import html as _html

    # Script/style bodies must go with their tags, not survive as text.
    text = _SCRIPT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    return " ".join(_html.unescape(text).split())


def domain_of(url: str) -> str:
    from urllib.parse import urlsplit
    return urlsplit(url).netloc.lower().removeprefix("www.")


def _default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if is_dataclass(o):
        return asdict(o)
    if hasattr(o, "value"):        # Enum
        return o.value
    if isinstance(o, set):
        return sorted(o)
    raise TypeError(f"not serialisable: {type(o)}")


def snapshot(obj: Any, path: Path, label: str = "") -> None:
    """Dump any stage's output to disk so a run can be replayed or diffed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False, default=_default)
    if label:
        log("snapshot", f"{label} -> {path.name}")


def truncate(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "\u2026"
