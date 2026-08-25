"""Per-model API budgeting and pacing.

Free-tier limits are per model and differ by more than an order of magnitude
(gemini-3.6-flash allows 20 requests a day; gemini-3.5-flash-lite allows 500),
so a single global counter cannot express the constraint. This tracks each
model separately and hands out the next usable one.

Three limits, each with a different failure meaning:

  RPD       requests per UTC day. Tracked persistently across runs, because
            the quota is per day and this project runs more than once.
  RPM       requests per minute. Enforced by sleeping, not by failing — the
            calls are wanted, they just have to be spread out.
  per-run   a self-imposed cap so one run cannot eat the whole day.

Hitting any of these is normal: the caller falls back to extractive summaries.
Only a provider 429 is treated as a stop condition.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from .config import CACHE_DIR, settings
from .utils import log

USAGE_PATH = CACHE_DIR / "usage.json"

# Blocks that must survive within one process but NOT across days.
#
# s5.run(), s5.top_up() and s6b_judge.run() each construct their own Budget(),
# which re-reads usage.json. A 503 block is deliberately persist=False (a
# transient overload should not cost you the model tomorrow), but that meant
# it evaporated the moment the next stage built a new Budget — so top_up
# cheerfully retried the same dead flagship and burned four more calls.
# Measured: 4 of 11 stage-5 calls returned nothing for exactly this reason.
_RUN_BLOCKS: dict[str, str] = {}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load() -> dict:
    if USAGE_PATH.exists():
        try:
            return json.loads(USAGE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("budget", "! usage counter corrupt, starting fresh")
    return {}


class ModelBudget:
    """One model's allowance for today."""

    def __init__(self, spec: dict, used_today: int, reserve: float) -> None:
        self.model: str = spec["model"]
        self.rpm: int = spec.get("rpm", 5)
        self.rpd: int = spec.get("rpd", 20)
        self.per_run: int = spec.get("max_batches_per_run", 5)
        # Some models have a much lower TPM than the Gemini flash family
        # (Gemma is 16K against 250K), so a batch sized for Flash would blow
        # their tokens-per-minute ceiling. None means "use the global size".
        self.batch_size: int | None = spec.get("batch_size")

        # Hold a slice of the daily quota back so an ad-hoc `--test-llm` or a
        # second run later in the day is not locked out by the first.
        self.usable_rpd: int = max(1, int(self.rpd * (1 - reserve)))

        self.used_today: int = used_today
        self.this_run: int = 0
        self.blocked: str = ""          # set when the provider itself refuses

    @property
    def remaining(self) -> int:
        if self.blocked:
            return 0
        return max(0, min(self.usable_rpd - self.used_today,
                          self.per_run - self.this_run))

    @property
    def min_interval(self) -> float:
        """Seconds between calls to stay inside RPM, with a little slack."""
        return 60.0 / max(1, self.rpm) * 1.1

    def __str__(self) -> str:
        return (f"{self.model} {self.used_today}/{self.usable_rpd} today "
                f"(cap {self.rpd}), {self.remaining} left")


class Budget:
    """The full tier ladder, plus pacing."""

    def __init__(self) -> None:
        cfg = settings()["llm"]
        bcfg = cfg.get("budget", {})
        self.enabled: bool = bcfg.get("enabled", True)
        self.pace: bool = bcfg.get("pace", True)
        reserve: float = bcfg.get("reserve_fraction", 0.15)

        self._data = _load()
        day = self._data.get(_today(), {})
        blocked_today = set(day.get("_blocked", []))
        self.tiers = []
        for spec in cfg["tiers"]:
            tier = ModelBudget(spec, day.get(spec["model"], 0), reserve)
            if tier.model in _RUN_BLOCKS:
                tier.blocked = _RUN_BLOCKS[tier.model]
            elif tier.model in blocked_today:
                # Refused by the provider earlier today. Retrying costs four
                # attempts with exponential backoff before failing the same way.
                tier.blocked = "quota refused earlier today"
            self.tiers.append(tier)
        if blocked_today:
            log("budget", f"skipping {len(blocked_today)} model(s) already "
                          f"refused today: {', '.join(sorted(blocked_today))}")
        self._last_call: dict[str, float] = {}
        self.stopped_reason: str = ""

    # --- selection -------------------------------------------------------

    def next_model(self, prefer: str = "premium") -> ModelBudget | None:
        """Pick a model for the next call.

        `prefer` routes by what the work is worth, not just by batch order:

          premium  the strongest model with quota. For stories a person will
                   actually read at the top of the page.
          bulk     the cheapest model with quota, searched from the bottom.
                   For low-frequency material — social posts, repository
                   listings, minor items — where a weaker summary is fine and
                   the flagship's 20-a-day allowance would be wasted.

        Both fall back through the whole ladder, so "bulk" still gets served
        by a flagship model if the cheap tiers are exhausted, and vice versa.
        """
        # `enabled: false` disables the QUOTA accounting, not the tier ladder.
        # Returning tiers[0] unconditionally ignored `blocked`, so a caller
        # looping until next_model() returns None — which _generate does —
        # would spin forever against a permanently failing model.
        order = self.tiers if prefer == "premium" else list(reversed(self.tiers))
        if not self.enabled:
            for tier in order:
                if not tier.blocked:
                    return tier
            self.stopped_reason = "every model has failed this run"
            return None
        for tier in order:
            if tier.remaining > 0:
                return tier

        self.stopped_reason = ("all model quotas exhausted for today — "
                               "remaining items use extractive summaries")
        return None

    def capacity(self) -> int:
        """Total batches the ladder can serve this run."""
        if not self.enabled:
            return 10 ** 6
        return sum(t.remaining for t in self.tiers)

    # --- execution -------------------------------------------------------

    def wait(self, tier: ModelBudget) -> None:
        """Sleep if needed so this model's RPM is not exceeded."""
        if not self.pace:
            return
        last = self._last_call.get(tier.model)
        if last is None:
            return
        gap = tier.min_interval - (time.monotonic() - last)
        if gap > 0:
            log("budget", f"pacing {gap:.0f}s for {tier.model} (RPM {tier.rpm})")
            time.sleep(gap)

    def record(self, tier: ModelBudget) -> None:
        tier.this_run += 1
        tier.used_today += 1
        self._last_call[tier.model] = time.monotonic()

        day = self._data.setdefault(_today(), {})
        day[tier.model] = tier.used_today          # "_blocked" key is preserved
        for old in sorted(self._data)[:-14]:       # keep a fortnight
            self._data.pop(old, None)
        USAGE_PATH.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    def block(self, tier: ModelBudget, why: str, persist: bool = False) -> None:
        """Stop using this model; fall through to the next tier.

        `persist` writes the block into the day's usage file. Reserve it for
        quota refusals (429), which last until the quota resets, and never
        use it for transient failures (503 overload), which do not.

        This matters because the local counter is only a self-limit: it
        cannot see calls made before it existed, or by anything else sharing
        the key. On a live run gemini-3.6-flash returned 429 after just 4
        recorded calls, because ~16 earlier calls were invisible to it. The
        provider's refusal is authoritative; the counter is a guess.
        """
        tier.blocked = why
        _RUN_BLOCKS[tier.model] = why      # survives the next Budget() in this run
        log("budget", f"! {tier.model} blocked: {why}")
        if persist:
            day = self._data.setdefault(_today(), {})
            day.setdefault("_blocked", []).append(tier.model)
            USAGE_PATH.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            log("budget", f"  {tier.model} stays blocked for the rest of the UTC day")

    # --- reporting -------------------------------------------------------

    def report(self) -> str:
        return " | ".join(str(t) for t in self.tiers)
