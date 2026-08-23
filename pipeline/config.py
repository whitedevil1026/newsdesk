"""Config loading. One place that knows where files live."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
CACHE_DIR = DATA_DIR / "cache"
SITE_DIR = ROOT / "site"

for _d in (DATA_DIR, RAW_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=None)
def _load(name: str) -> dict[str, Any]:
    with open(CONFIG_DIR / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def settings() -> dict[str, Any]:
    return _load("settings.yaml")


def feeds() -> dict[str, list[dict[str, Any]]]:
    return _load("feeds.yaml")


def interests() -> dict[str, Any]:
    return _load("interests.yaml")


ENV_FILE = ROOT / ".env"


def _load_dotenv() -> dict[str, str]:
    """Read KEY=value pairs from .env, if it exists.

    Secrets live here rather than in config/*.yaml for one reason: .env is
    gitignored and the YAML files are committed. A key in settings.yaml would
    be pushed to GitHub the first time the workflow ran.

    The real environment always wins, so GitHub Actions injecting a secret
    overrides whatever a stray local .env happens to contain.
    """
    if not ENV_FILE.exists():
        return {}

    out: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Tolerate the shapes people actually paste: quotes, spaces, export.
        key = key.strip().removeprefix("export ").strip()
        out[key] = value.strip().strip('"').strip("'")
    return out


def api_key() -> str | None:
    """LLM key: real environment first, then .env. Never from committed config."""
    name = settings()["llm"]["api_key_env"]
    return os.environ.get(name) or _load_dotenv().get(name) or None
