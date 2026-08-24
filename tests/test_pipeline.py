"""Unit tests for the parts where a silent regression would be invisible.

Deliberately narrow: these cover the pure logic (url canonicalisation,
clustering thresholds, scoring behaviour), not the network stages.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.models import Article, Cluster, canonical_url
from pipeline.s2_clean import _cluster
from pipeline.utils import overlap, strip_html, tokens


def _art(title, domain="a.com", tier="B", primary=False, aggregator=False):
    return Article(url=f"https://{domain}/{abs(hash(title))}", title=title,
                   source=domain, domain=domain, category="tech_ai", tier=tier,
                   published=datetime.now(timezone.utc), is_primary=primary,
                   is_aggregator=aggregator)


class TestCanonicalUrl:
    def test_strips_tracking_params(self):
        a = canonical_url("https://x.com/a?utm_source=rss&id=7")
        b = canonical_url("https://www.x.com/a/?id=7")
        assert a == b, "same article must canonicalise identically"

    def test_keeps_meaningful_params(self):
        assert "id=7" in canonical_url("https://x.com/a?id=7")


class TestOverlap:
    def test_asymmetric_headlines_still_match(self):
        # The exact failure that produced zero clusters on the first live run.
        short = tokens("Girl, 17, killed in Swedish sword attack, police say")
        long_ = tokens("Swedish police probe online networks after teenage girl "
                       "killed in sword attack")
        assert overlap(short, long_) >= 0.55

    def test_unrelated_headlines_do_not_match(self):
        a = tokens("RBI imposes monetary penalty on a finance company")
        b = tokens("Fourteen killed in strike on Myanmar monastery")
        assert overlap(a, b) < 0.55

    def test_empty_is_zero(self):
        assert overlap(set(), {"a"}) == 0.0


class TestClustering:
    def test_same_story_across_outlets_merges(self):
        arts = [
            _art("TikTok agrees to $400 million US children's privacy settlement", "reuters.com"),
            _art("TikTok Agrees to $400 Million Settlement in U.S. Child Privacy Case", "nyt.com"),
            _art("Fourteen killed in strike on Myanmar monastery", "bbc.co.uk"),
        ]
        clusters = _cluster(arts, 0.55, 3)
        sizes = sorted(len(c.articles) for c in clusters)
        assert sizes == [1, 2], f"expected one pair + one single, got {sizes}"

    def test_min_shared_tokens_blocks_thin_matches(self):
        # Two short headlines sharing one word must not merge.
        arts = [_art("Apple ships update", "a.com"), _art("Google ships update", "b.com")]
        clusters = _cluster(arts, 0.55, 3)
        assert len(clusters) == 2


class TestClusterLead:
    def test_aggregator_never_leads(self):
        """Aggregator links are redirect stubs — the lead URL is what is clicked."""
        agg = _art("Story", "news.google.com", tier="A", aggregator=True)
        real = _art("Story", "bbc.co.uk", tier="B")
        assert Cluster(key="k", articles=[agg, real]).lead is real

    def test_independent_outlets_counts_domains(self):
        c = Cluster(key="k", articles=[_art("x", "a.com"), _art("x", "b.com"),
                                       _art("x", "a.com")])
        assert c.independent_outlets == 2


class TestStripHtml:
    def test_removes_tags_and_unescapes(self):
        assert strip_html("<p>A &amp; B</p>") == "A & B"

    def test_drops_script_content(self):
        assert "evil" not in strip_html("<script>evil()</script>ok")


class TestScoring:
    """The regression that mattered most: trustworthy must not mean important."""

    def test_primary_source_does_not_inflate_interest(self):
        from pipeline.config import settings
        from pipeline.s4_corroborate import _interest_score

        icfg = settings()["interest"]
        title = "Cabinet approves four multitracking projects"
        plain = Cluster(key="a", articles=[_art(title, "x.com")])
        official = Cluster(key="b", articles=[_art(title, "pib.gov.in", tier="A",
                                                   primary=True)])
        assert _interest_score(plain, icfg) == _interest_score(official, icfg), \
            "an official source is a reason to trust a story, not to care about it"


@pytest.fixture(autouse=True)
def _isolated_usage(tmp_path, monkeypatch):
    """Redirect the usage counter at a temp file for every test in this module.

    Without this the tests delete the REAL data/cache/usage.json — which they
    did, wiping live quota state out from under a run that was in flight.
    Tests must never touch production data.
    """
    import pipeline.budget as budget
    monkeypatch.setattr(budget, "USAGE_PATH", tmp_path / "usage.json")


class TestBudgetKillSwitch:
    """The stop condition has to be provable, not assumed.

    These run against the real settings ladder but never touch the network
    and never touch the real usage counter (see the fixture above).
    """

    def _fresh(self):
        from pipeline.budget import Budget, USAGE_PATH
        USAGE_PATH.unlink(missing_ok=True)
        return Budget()

    def test_ladder_is_ordered_best_first(self):
        b = self._fresh()
        assert b.tiers[0].model.startswith("gemini-3."), b.tiers[0].model
        # Cheap high-quota models must sit at the bottom, not the top.
        assert b.tiers[-1].rpd > b.tiers[0].rpd

    def test_exhaustion_stops_cleanly(self):
        b = self._fresh()
        for tier in b.tiers:
            tier.blocked = "test"
        assert b.capacity() == 0
        assert b.next_model() is None
        assert "exhausted" in b.stopped_reason

    def test_reserve_protects_a_slice_of_quota(self):
        """A full run must never lock out an ad-hoc --test-llm."""
        b = self._fresh()
        assert b.tiers[0].usable_rpd < b.tiers[0].rpd

    def test_per_run_cap_is_below_daily_cap(self):
        b = self._fresh()
        for tier in b.tiers:
            assert tier.per_run <= tier.usable_rpd, tier.model

    def test_quota_refusal_persists_but_overload_does_not(self):
        import pipeline.budget as _b
        from pipeline.budget import Budget
        USAGE_PATH = _b.USAGE_PATH
        b = self._fresh()
        first = b.next_model()
        b.block(first, "429", persist=True)
        assert Budget().tiers[0].blocked, "a 429 must survive into the next run"

        USAGE_PATH.unlink(missing_ok=True)
        b2 = Budget()
        b2.block(b2.next_model(), "503 overload")      # transient
        assert not Budget().tiers[0].blocked, "a 503 must not persist"
        USAGE_PATH.unlink(missing_ok=True)


class TestVerdicts:
    """Verdicts must actually discriminate — the previous scheme put 96% of
    items on one label, which is true but useless."""

    def _item(self, tier="B", outlets=1, primary=False):
        from datetime import datetime, timezone
        from pipeline.models import Cluster
        arts = [_art("Some headline about a thing", f"d{n}.com", tier=tier)
                for n in range(outlets)]
        if primary:
            arts[0].is_primary = True
        return Cluster(key="k", articles=arts)

    def test_primary_anchor_is_verified(self):
        from pipeline.s4_corroborate import run
        from pipeline.models import Verdict
        out = run([self._item(primary=True)])
        assert out[0].verdict is Verdict.VERIFIED

    def test_three_outlets_is_verified(self):
        from pipeline.s4_corroborate import run
        from pipeline.models import Verdict
        out = run([self._item(outlets=3)])
        assert out[0].verdict is Verdict.VERIFIED

    def test_two_outlets_is_corroborated(self):
        from pipeline.s4_corroborate import run
        from pipeline.models import Verdict
        out = run([self._item(outlets=2)])
        assert out[0].verdict is Verdict.CORROBORATED

    def test_known_publication_beats_a_random_blog(self):
        """The distinction the old single_source label threw away."""
        from pipeline.s4_corroborate import run
        from pipeline.models import Verdict
        established = run([self._item(tier="B")])[0]
        blog = run([self._item(tier="C")])[0]
        assert established.verdict is Verdict.ESTABLISHED
        assert blog.verdict is Verdict.UNVERIFIED


class TestNetGuard:
    """Outbound fetch safety.

    Article links come from feeds, so they are attacker-influenceable. These
    are offline tests: no request is ever made.
    """

    def test_file_scheme_is_refused(self):
        """urlopen honours file:// — verified, not assumed. A feed link of
        file:///.../.env would otherwise be read and could reach a summary."""
        from pipeline.netguard import check
        assert check("file:///C:/Users/me/.env") is not None

    def test_non_http_schemes_refused(self):
        from pipeline.netguard import check
        for url in ("ftp://example.com/x", "gopher://example.com",
                    "data:text/html,<script>", "javascript:alert(1)"):
            assert check(url) is not None, url

    def test_cloud_metadata_endpoint_blocked(self):
        """169.254.169.254 is the cloud metadata service on CI runners."""
        from pipeline.netguard import check
        assert check("http://169.254.169.254/latest/meta-data/") is not None

    def test_private_and_loopback_blocked(self):
        from pipeline.netguard import check
        for url in ("http://127.0.0.1:8777/", "http://192.168.1.1/admin",
                    "http://10.0.0.5/", "http://localhost/"):
            assert check(url) is not None, url

    def test_ordinary_public_urls_allowed(self):
        from pipeline.netguard import check
        assert check("https://www.bbc.co.uk/news") is None

    def test_scheme_only_mode_skips_dns(self):
        """The cheap path still catches the scheme abuses, which is what the
        per-article hot loop needs."""
        from pipeline.netguard import check
        assert check("file:///etc/passwd", resolve_dns=False) is not None
        assert check("https://example.com", resolve_dns=False) is None


class TestNoSecretsInRepo:
    def test_env_is_gitignored(self):
        """A key reaching a git remote is the one unrecoverable mistake here."""
        root = Path(__file__).resolve().parent.parent
        ignored = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
        # Must be its own line: a trailing comment makes the pattern literal
        # and silently match nothing. That exact bug tracked 4.1 MB of
        # snapshots before it was caught.
        assert ".env" in [line.strip() for line in ignored]

    def test_no_key_shaped_strings_in_config(self):
        root = Path(__file__).resolve().parent.parent
        import re
        pat = re.compile(r"AIza[0-9A-Za-z_-]{20,}|AQ\.[A-Za-z0-9]{20,}"
                         r"|ghp_[A-Za-z0-9]{20,}")
        for path in (root / "config").glob("*.yaml"):
            assert not pat.search(path.read_text(encoding="utf-8")), path


class TestPromptHardening:
    """The prompt must structurally separate instructions from content.

    Live injection tests against three model tiers all resisted, but that is
    a property of today's models. These assert the defence is present in the
    prompt itself.
    """

    def test_article_text_is_delimited(self):
        from pipeline.llm_gemini import SYSTEM
        import pipeline.llm_gemini as m
        assert "UNTRUSTED INPUT" in SYSTEM
        assert "BEGIN ARTICLE" in Path(m.__file__).read_text(encoding="utf-8")

    def test_system_prompt_refuses_embedded_instructions(self):
        from pipeline.llm_gemini import SYSTEM
        # Collapse whitespace first: the prompt is hard-wrapped, so a phrase
        # can straddle a newline and a naive substring check misses it.
        low = " ".join(SYSTEM.lower().split())
        assert "never instructions" in low or "not instructions" in low
        assert "do not comply" in low

    def test_judge_is_hardened_too(self):
        from pipeline.s6b_judge import JUDGE_SYSTEM
        assert "untrusted" in JUDGE_SYSTEM.lower()


class TestShortlistCoverage:
    """The shortlist must be wider than the publish cut, per category.

    This is the regression that produced 26 of 40 published cards carrying an
    extractive fallback instead of a real summary: the shortlist allocated
    each category exactly its publish quota, so anything promoted by the
    model's importance score had never been summarised in the first place.
    """

    def _items(self, per_cat=60):
        from pipeline.models import Item
        out = []
        for cat in ("tech_ai", "cyber_attacks", "cyber_tools",
                    "markets", "india_world"):
            for n in range(per_cat):
                it = Item(cluster_key=f"{cat}{n}", title="t", category=cat)
                it.blend = float(per_cat - n)
                out.append(it)
        return out

    def test_allocation_exceeds_publish_quota(self):
        """Each category must get more slots than it can publish, or the
        model's reordering has nowhere to promote from."""
        from pipeline.config import settings
        import collections
        from pipeline.s5_summarize import _shortlist

        cfg = settings()
        quota = cfg["window"]["per_category_max"]
        short, _ = _shortlist(self._items(),
                              cfg["llm"]["max_items_summarized"], quota,
                              cfg["llm"].get("shortlist_multiplier", 2.0))
        per = collections.Counter(i.category for i in short)
        for cat, n in per.items():
            assert n > quota, f"{cat}: {n} slots vs publish quota {quota}"

    def test_shortlist_is_not_starved_by_a_dominant_category(self):
        """A category with 10x the volume must not consume the whole budget."""
        import collections
        from pipeline.models import Item
        from pipeline.s5_summarize import _shortlist

        items = self._items(per_cat=5)
        for n in range(300):                       # tech_ai floods the harvest
            it = Item(cluster_key=f"flood{n}", title="t", category="tech_ai")
            it.blend = 999.0
            items.append(it)

        short, _ = _shortlist(items, 150, 12, 2.0)
        per = collections.Counter(i.category for i in short)
        for cat in ("markets", "cyber_tools", "india_world"):
            assert per[cat] == 5, f"{cat} starved: {per[cat]}"
