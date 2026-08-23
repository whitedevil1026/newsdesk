"""Unit tests for the parts where a silent regression would be invisible.

Deliberately narrow: these cover the pure logic (url canonicalisation,
clustering thresholds, scoring behaviour), not the network stages.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
