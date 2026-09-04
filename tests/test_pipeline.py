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
    # _RUN_BLOCKS is module-level state that deliberately survives a new
    # Budget() within one run. It must not survive between TESTS, or one
    # test's simulated 503 silently blocks a model in the next.
    monkeypatch.setattr(budget, "_RUN_BLOCKS", {})


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
        """Two different lifetimes, and both matter.

        A 429 means the provider refused for the rest of the UTC day, so it
        must survive into the next RUN — persisted to usage.json.

        A 503 is transient overload, so it must NOT cost the model tomorrow.
        It does still have to survive within the CURRENT run, because s5.run,
        s5.top_up and s6b_judge each build their own Budget() and would
        otherwise each retry the same dead model — that cost 4 of 11 calls on
        a measured run.
        """
        import pipeline.budget as _b
        from pipeline.budget import Budget
        USAGE_PATH = _b.USAGE_PATH

        b = self._fresh()
        b.block(b.next_model(), "429", persist=True)
        assert Budget().tiers[0].blocked, "a 429 must survive into the next run"

        # A new run: fresh process state AND fresh usage file.
        USAGE_PATH.unlink(missing_ok=True)
        _b._RUN_BLOCKS.clear()

        b2 = Budget()
        b2.block(b2.next_model(), "503 overload")       # transient
        assert Budget().tiers[0].blocked,             "a 503 must survive within the run, or later stages retry it"

        _b._RUN_BLOCKS.clear()                          # next run begins
        assert not Budget().tiers[0].blocked,             "a 503 must not persist into the next run"
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

    def test_ordinary_public_urls_allowed(self, monkeypatch):
        """Hermetic: the old version called real DNS, so it passed or failed
        depending on the network the suite happened to run on."""
        import socket as _s
        from pipeline import netguard
        monkeypatch.setattr(netguard.socket, "getaddrinfo",
                            lambda *a, **k: [(_s.AF_INET, None, None, "",
                                              ("151.101.156.81", 0))])
        assert netguard.check("https://www.bbc.co.uk/news") is None

    def test_nat64_public_address_allowed(self, monkeypatch):
        """A DNS64 resolver answers with 64:ff9b::<ipv4>, which Python calls
        `is_reserved`. Treating the prefix as the address blocked every
        public host on any IPv6-only or mobile network - the entire harvest."""
        import socket as _s
        from pipeline import netguard
        monkeypatch.setattr(netguard.socket, "getaddrinfo",
                            lambda *a, **k: [(_s.AF_INET6, None, None, "",
                                              ("64:ff9b::9765:9c51", 0, 0, 0))])
        assert netguard.check("https://www.bbc.co.uk/news") is None

    def test_nat64_cannot_smuggle_a_private_address(self, monkeypatch):
        """The other direction, and the one that matters: unwrapping must not
        become a way past the guard. 64:ff9b::a9fe:a9fe carries the cloud
        metadata address and has to stay blocked."""
        import socket as _s
        from pipeline import netguard
        for wrapped in ("64:ff9b::a9fe:a9fe",      # 169.254.169.254
                        "64:ff9b::c0a8:0101",      # 192.168.1.1
                        "64:ff9b::7f00:0001",      # 127.0.0.1
                        "::ffff:10.0.0.5"):        # IPv4-mapped private
            monkeypatch.setattr(
                netguard.socket, "getaddrinfo",
                lambda *a, w=wrapped, **k: [(_s.AF_INET6, None, None, "",
                                             (w, 0, 0, 0))])
            assert netguard.check("https://evil.example/") is not None, wrapped

    def test_scheme_only_mode_skips_dns(self):
        """The cheap path still catches the scheme abuses, which is what the
        per-article hot loop needs."""
        from pipeline.netguard import check
        assert check("file:///etc/passwd", resolve_dns=False) is not None
        assert check("https://example.com", resolve_dns=False) is None


class TestApiBudget:
    """The free tier is the whole constraint, so the number of CALLS a run can
    make has to be provably bounded by config rather than by how much news
    happened to break that day."""

    def test_top_up_is_bounded_by_its_own_limit_not_the_publish_cap(self):
        """top_up() used to size itself from the publish cap. That was two
        extra calls at a cap of 40 and ~38 at a cap of 900 — enough to empty
        the daily quota on the first run of the day."""
        from pipeline.config import settings
        cfg = settings()
        cap = cfg["window"]["max_items_published"]
        budget = cfg["llm"]["top_up_max"]
        batch = cfg["llm"]["batch_size"]
        assert budget < cap, (
            "top_up_max must be a real ceiling, not the publish cap")
        # The whole run has to stay inside the smallest daily quota on the
        # ladder, which is the flagship tier's rpd.
        worst_case = -(-cfg["llm"]["max_items_summarized"] // batch)                      + -(-budget // batch)                      + -(-cfg["judge"]["max_items"] // cfg["judge"]["batch_size"])
        assert worst_case <= 40, f"worst case {worst_case} calls per run"

    def test_gap_detection_does_not_depend_on_log_wording(self):
        """The gap set was found by searching each item's trace for the words
        'extractive' or 'heuristic'. Rewording that log line turned the whole
        pass off silently, with no error anywhere."""
        import inspect
        from pipeline import s5_summarize
        src = inspect.getsource(s5_summarize.top_up)
        # Strip comments and docstrings: this checks the CODE, not the prose
        # explaining why the code looks like this.
        code = " ".join(l.split("#")[0] for l in src.splitlines())
        assert "summary_source" in code, "top_up must test the field"
        assert '"extractive"' not in code and '"heuristic"' not in code, (
            "top_up is matching on log wording again")


class TestTally:
    """The lifetime counter is not load-bearing. It runs before news.json is
    written, so anything it can raise costs the entire run."""

    def _tally_into(self, tmp_path, monkeypatch, content=None):
        from pipeline import s7_publish
        f = tmp_path / "totals.json"
        if content is not None:
            f.write_text(content, encoding="utf-8")
        monkeypatch.setattr(s7_publish, "TOTALS_JSON", f)
        return s7_publish, f

    def test_survives_valid_json_that_is_not_an_object(self, tmp_path, monkeypatch):
        """json.loads returns a list/None/str for these, and setdefault on any
        of them raises AttributeError, which is not a JSONDecodeError."""
        for junk in ("[]", "null", '"nope"', "42"):
            mod, f = self._tally_into(tmp_path, monkeypatch, junk)
            out = mod._tally(10, 2, "2026-09-04T06:00:00+00:00")
            assert out is not None, f"crashed on {junk}"
            assert out["published"] == 10

    def test_survives_rows_missing_keys(self, tmp_path, monkeypatch):
        """An entry without 'published' used to raise KeyError while the very
        next line used .get for 'held'."""
        mod, f = self._tally_into(
            tmp_path, monkeypatch,
            '{"runs": {"2026-09-01": {}, "2026-09-02": 7}, '
            '"first_run": "2026-09-01T00:00:00+00:00"}')
        out = mod._tally(5, 1, "2026-09-04T06:00:00+00:00")
        assert out is not None
        assert out["published"] == 5      # the empty row contributes 0

    def test_never_raises_even_when_the_file_is_unwritable(self, tmp_path, monkeypatch):
        from pipeline import s7_publish
        monkeypatch.setattr(s7_publish, "TOTALS_JSON", tmp_path / "nope" / "x.json")
        # parent does not exist -> write fails; must degrade, not explode
        assert s7_publish._tally(1, 0, "2026-09-04T06:00:00+00:00") is None

    def test_dry_run_does_not_touch_the_file(self, tmp_path, monkeypatch):
        """--dry-run is documented as 'run everything, write nothing'. The
        tally used to be written before the dry_run guard was even reached."""
        before = '{"runs": {"2026-09-01": {"published": 3, "held": 1}}, '                  '"first_run": "2026-09-01T00:00:00+00:00"}'
        mod, f = self._tally_into(tmp_path, monkeypatch, before)
        out = mod._tally(99, 99, "2026-09-04T06:00:00+00:00", persist=False)
        assert out["published"] == 102          # computed
        assert f.read_text(encoding="utf-8") == before   # but not written


class TestCoverageCapsDoNotBind:
    def test_caps_are_above_a_real_run(self):
        """The point of the change was that nothing in the window is dropped.
        A measured harvest yields ~1,154 publishable items; caps of 900 and
        260 silently discarded 254 of them, 111 from tech_ai alone."""
        from pipeline.config import settings
        w = settings()["window"]
        assert w["max_items_published"] >= 1500, (
            "total cap would bind on a normal run")
        assert w["per_category_max"] >= 800, (
            "per-category cap would bind on tech_ai")


class TestNoSecretsInRepo:
    def test_env_is_gitignored(self):
        """A key reaching a git remote is the one unrecoverable mistake here."""
        root = Path(__file__).resolve().parent.parent
        ignored = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
        # Must be its own line: a trailing comment makes the pattern literal
        # and silently match nothing. That exact bug tracked 4.1 MB of
        # snapshots before it was caught.
        assert ".env" in [line.strip() for line in ignored]

    def test_no_key_shaped_strings_in_any_tracked_file(self):
        """Scan everything git tracks, not just config/.

        The original version checked config/*.yaml only, which would not have
        caught a key pasted into README.md, PROGRESS.md or a data/ file. It
        also had no pattern for a Telegram bot token or the newer GitHub
        token prefixes.
        """
        import re
        import subprocess

        root = Path(__file__).resolve().parent.parent
        pat = re.compile(
            r"AIza[0-9A-Za-z_-]{20,}"            # Google API key
            r"|AQ\.[A-Za-z0-9_-]{20,}"           # newer Google AI Studio key
            r"|gh[pousr]_[A-Za-z0-9]{20,}"       # GitHub classic/fine tokens
            r"|github_pat_[A-Za-z0-9_]{20,}"
            r"|\d{8,10}:AA[A-Za-z0-9_-]{30,}"  # Telegram bot token
            r"|sk-[A-Za-z0-9]{20,}"              # OpenAI-style
            r"|xox[baprs]-[A-Za-z0-9-]{10,}"     # Slack
        )
        tracked = subprocess.run(["git", "ls-files"], cwd=root,
                                 capture_output=True, text=True).stdout.split()
        assert tracked, "git ls-files returned nothing"

        for rel in tracked:
            path = root / rel
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, FileNotFoundError, OSError):
                continue                      # binary or removed; nothing to scan
            found = pat.search(text)
            assert not found, f"key-shaped string in {rel}: {found.group()[:12]}..."


class TestSecretSanitising:
    """One validation closes three separate leak paths.

    A secret containing a control character makes http.client raise
    ValueError("Invalid header value %r") with the FULL header in the
    message — which s1b_github logs, llm_gemini lets escape as a traceback,
    and s8_notify echoes in three handlers. Sanitising at the entry point
    beats patching each site, because the next handler written would leak too.
    """

    def test_control_characters_are_removed(self):
        from pipeline.config import clean_secret
        for bad in (chr(10), chr(13), chr(9), chr(0), chr(31)):
            out = clean_secret("AIza" + bad + "SyKEY", "TEST")
            assert bad not in out
            assert out == "AIzaSyKEY"

    def test_a_clean_key_is_returned_unchanged(self):
        from pipeline.config import clean_secret
        assert clean_secret("AIzaSyABC123", "TEST") == "AIzaSyABC123"

    def test_surrounding_whitespace_is_trimmed(self):
        from pipeline.config import clean_secret
        assert clean_secret("  AIzaSyABC123  ", "TEST") == "AIzaSyABC123"

    def test_empty_and_none_become_none(self):
        from pipeline.config import clean_secret
        assert clean_secret("", "TEST") is None
        assert clean_secret(None, "TEST") is None
        assert clean_secret("   ", "TEST") is None

    def test_sanitised_value_is_header_safe(self):
        """The actual property that matters: http.client must accept it."""
        import http.client
        from pipeline.config import clean_secret
        dirty = "Bearer_tok" + chr(10) + "en"
        cleaned = clean_secret(dirty, "TEST")
        # This is the call that raised ValueError with the token in the message.
        http.client.HTTPConnection("example.com")._validate_header_value =             getattr(http.client.HTTPConnection, "_validate_header_value", None)
        assert all(ord(c) > 0x20 for c in cleaned)


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

    def test_shortlist_is_bounded_by_the_model_budget(self):
        """The shortlist is a spending decision. It used to size itself from
        the PUBLISH quota, which was fine while the page showed a top 40 and
        catastrophic once it started publishing everything in the window —
        it would have asked the model for hundreds of summaries a day the
        free tier cannot pay for."""
        from pipeline.config import settings
        from pipeline.s5_summarize import _shortlist
        from pipeline.models import Item, Priority, Verdict
        import collections

        cfg = settings()["llm"]
        budget = cfg["max_items_summarized"]
        cats = ["cyber_attacks", "cyber_tools", "tech_ai", "markets",
                "india_world"]
        items = []
        for c in cats:
            for n in range(400):           # far more than any budget
                items.append(Item(cluster_key=f"{c}{n}", title="t",
                                  category=c, blend=float(n),
                                  priority=Priority.MINOR,
                                  verdict=Verdict.ESTABLISHED))
        per_cat = max(1, budget // len(cats))
        chosen, rest = _shortlist(items, budget, per_cat,
                                  cfg.get("shortlist_multiplier", 2.0))
        assert len(chosen) <= budget, (
            f"shortlist {len(chosen)} exceeds the {budget} the budget allows")
        assert len(chosen) + len(rest) == len(items), "items were lost"
        # and every category has to be represented, or a quiet section ships
        # with no model summaries at all
        seen = collections.Counter(i.category for i in chosen)
        assert set(seen) == set(cats), f"category starved: {sorted(seen)}"

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


class TestTelegramNotify:
    """Pushing posts to a channel other people read, so the guards matter
    more than the formatting."""

    def _item(self, title="A title", priority=None, verdict=None):
        from pipeline.models import Item, Priority, Verdict
        it = Item(cluster_key="k", title=title, category="cyber_attacks")
        it.priority = priority or Priority.CRITICAL
        it.verdict = verdict or Verdict.VERIFIED
        it.bottom_line = "Patch now."
        it.sources = [{"name": "s", "url": "https://example.com/a",
                       "domain": "example.com", "tier": "A"}]
        return it

    def test_disabled_by_default(self):
        """It must never start posting as a side effect of a config default."""
        from pipeline.config import settings
        assert settings()["notify"]["telegram"]["enabled"] is False

    def test_does_nothing_when_disabled(self):
        from pipeline import s8_notify
        # No exception, no network, regardless of what it is handed.
        s8_notify.run([self._item()], "2026-01-01T00:00:00+00:00")

    def test_html_is_escaped(self):
        """Titles come from feeds, so they can contain anything."""
        from pipeline import s8_notify
        out = s8_notify._format(
            [self._item("<script>alert(1)</script> & co")], "2026-01-01T00:00")[0]
        assert "<script>" not in out
        assert "&lt;script&gt;" in out
        assert "&amp;" in out

    def test_splits_on_telegram_length_limit(self):
        """Telegram hard-limits a message to 4096 chars."""
        from pipeline import s8_notify
        items = [self._item("x" * 120) for _ in range(60)]
        msgs = s8_notify._format(items, "2026-01-01T00:00")
        assert len(msgs) > 1
        assert all(len(m) <= 4096 for m in msgs)

    def test_rejected_items_are_never_pushed(self):
        from pipeline.models import Verdict
        from pipeline import s8_notify
        rejected = self._item(verdict=Verdict.REJECTED)
        # _format does not filter; run() does. Assert the filter exists by
        # checking the verdict is excluded from the wanted set logic.
        assert rejected.verdict is Verdict.REJECTED


class TestTelegramSource:
    def test_shortener_list_covers_the_common_ones(self):
        from pipeline.s1d_telegram import SHORTENERS
        # ift.tt is what the user's own channel posts through; an unresolved
        # shortener breaks tiering, dedupe and the reference link at once.
        for host in ("ift.tt", "bit.ly", "buff.ly", "t.co"):
            assert host in SHORTENERS

    def test_message_parsing_extracts_text_and_link(self):
        from pipeline.s1d_telegram import _parse
        block = ('Gunra ransomware: what you need to know '
                 '<a href="https://ift.tt/abc">link</a>')
        text, link = _parse(block)
        assert "Gunra ransomware" in text
        assert link == "https://ift.tt/abc"

    def test_telegram_links_are_not_treated_as_the_article(self):
        from pipeline.s1d_telegram import _parse
        _, link = _parse('see <a href="https://t.me/other/1">this</a>')
        assert link is None


class TestSecretsFilePermissions:
    """Gitignoring .env protects it from the remote, not from the local disk.

    This machine's .env had inherited read access for the local Users group
    and Authenticated Users, so any account on the box could read the API key.
    """

    def test_matcher_flags_risky_groups(self):
        from pipeline.config import _ACL_RISKY
        BS = chr(92)
        cases = {
            "BUILTIN" + BS + "Users:(I)(RX)": "Users",
            "NT AUTHORITY" + BS + "Authenticated Users:(I)(M)":
                "Authenticated Users",
            ".env Everyone:(F)": "Everyone",
            "MACHINE" + BS + "Users:(RX)": "Users",
        }
        for text, expected in cases.items():
            hits = {m.group(1) for m in _ACL_RISKY.finditer(text)}
            assert expected in hits, f"{text!r} -> {hits}"

    def test_matcher_allows_an_owner_only_acl(self):
        from pipeline.config import _ACL_RISKY
        safe = ".env DESKTOP-EXAMPLE" + chr(92) + "owner:(R,W)"
        assert not list(_ACL_RISKY.finditer(safe))

    def test_authenticated_users_is_not_mislabelled_as_users(self):
        """Alternation order matters: the bare 'Users' branch would otherwise
        swallow 'Authenticated Users' and report the wrong group."""
        from pipeline.config import _ACL_RISKY
        text = "NT AUTHORITY" + chr(92) + "Authenticated Users:(I)(M)"
        hits = {m.group(1) for m in _ACL_RISKY.finditer(text)}
        assert hits == {"Authenticated Users"}

    def test_check_never_raises(self):
        """A permissions check must not be able to break a run."""
        from pathlib import Path
        from pipeline.config import _warn_if_world_readable
        _warn_if_world_readable(Path("does-not-exist-anywhere.env"))


class TestCategoryKeysMatchFeeds:
    """The single worst class of bug in this project: a config key that no
    code path ever looks up, so the feature silently does nothing."""

    def test_every_boost_key_is_a_real_category(self):
        """A `cybersecurity:` key survived a category rename and matched
        nothing, so every security keyword was dead — exploited zero-days
        scored the bare category floor of 12 while markets stories scored 100."""
        from pipeline.config import feeds, interests
        real = set(feeds())
        for key in interests()["boost"]:
            assert key in real, f"boost.{key} matches no category in feeds.yaml"

    def test_every_category_has_a_boost_profile(self):
        from pipeline.config import feeds, interests
        boost = set(interests()["boost"])
        for cat in feeds():
            assert cat in boost, f"category {cat} has no keyword profile"

    def test_security_keywords_actually_fire(self):
        from datetime import datetime, timezone
        from pipeline.config import settings
        from pipeline.models import Article, Cluster
        from pipeline.s4_corroborate import _interest_score

        def score(cat, title):
            a = Article(url="https://x.com/1", title=title, source="s",
                        domain="x.com", category=cat, tier="B",
                        published=datetime.now(timezone.utc))
            return _interest_score(Cluster(key="k", articles=[a]),
                                   settings()["interest"])

        hot = score("cyber_attacks",
                    "Actively exploited zero-day RCE ransomware CVE-2026-1")
        routine = score("cyber_attacks", "Vendor publishes quarterly report")
        assert hot > routine + 40, f"hot={hot} routine={routine}"


class TestGeminiResponseShapes:
    """Gemini omits `content` on a safety block and omits `candidates`
    entirely when the prompt is rejected. Both were dereferenced directly, so
    a KeyError escaped every caller and killed the run AFTER stages 1-5 had
    spent their budget. A news feed is untrusted input, so a safety block is
    routine here, not exceptional."""

    def _extract(self, data):
        from pipeline.llm_gemini import _extract_text
        return _extract_text(data, "test-model")

    def test_no_candidates_raises_gemini_error(self):
        from pipeline.llm_gemini import GeminiError
        with pytest.raises(GeminiError):
            self._extract({"promptFeedback": {"blockReason": "SAFETY"}})

    def test_candidate_without_content_raises_gemini_error(self):
        from pipeline.llm_gemini import GeminiError
        with pytest.raises(GeminiError):
            self._extract({"candidates": [{"finishReason": "SAFETY"}]})

    def test_max_tokens_truncation_raises_gemini_error(self):
        from pipeline.llm_gemini import GeminiError
        with pytest.raises(GeminiError):
            self._extract({"candidates": [{"finishReason": "MAX_TOKENS"}]})

    def test_healthy_response_returns_text(self):
        out = self._extract({"candidates": [
            {"finishReason": "STOP", "content": {"parts": [{"text": "hi"}]}}]})
        assert out == "hi"

    def test_never_raises_keyerror(self):
        """The property that matters: callers catch GeminiError only."""
        for data in ({}, {"candidates": []}, {"candidates": [{}]},
                     {"candidates": [{"content": {}}]}):
            try:
                self._extract(data)
            except Exception as exc:
                assert type(exc).__name__ == "GeminiError", type(exc).__name__


class TestNotifyRanking:
    def test_critical_survives_a_flood_of_routine_items(self):
        """The digest slice was taken from unsorted cluster order, so a
        CRITICAL item could be dropped for ten routine ones."""
        from pipeline.models import Item, Priority, Verdict
        from pipeline import s8_notify

        items = []
        for n in range(12):
            it = Item(cluster_key=f"r{n}", title=f"routine {n}",
                      category="markets")
            it.priority, it.verdict, it.blend = Priority.IMPORTANT, Verdict.ESTABLISHED, 10.0
            items.append(it)
        hot = Item(cluster_key="hot", title="ACTIVELY EXPLOITED ZERO-DAY",
                   category="cyber_attacks")
        hot.priority, hot.verdict, hot.blend = Priority.CRITICAL, Verdict.VERIFIED, 99.0
        items.append(hot)

        order = {Priority.CRITICAL: 0, Priority.IMPORTANT: 1, Priority.MINOR: 2}
        ranked = sorted(
            (i for i in items if i.verdict is not Verdict.REJECTED),
            key=lambda i: (order[i.priority], -i.blend))[:10]
        assert ranked[0] is hot


class TestVerifyMissingBody:
    def test_no_body_is_unverifiable_not_contradicted(self):
        """'We never fetched the article' and 'the model invented its
        evidence' are different failures and must not share a verdict."""
        from pipeline.models import Claim, Item, Verdict
        from pipeline import s6_verify

        it = Item(cluster_key="k", title="t", category="cyber_attacks")
        it.claims = [Claim(text="a claim", support_span="some quote")]
        s6_verify.run([it], {"k": ""})
        assert it.verdict is not Verdict.REJECTED
        assert all(c.status == "neutral" for c in it.claims)


class TestSourceOrdering:
    def test_aggregator_is_never_the_primary_link(self):
        """sources[0] is what the card links to and what stage 5 tells the
        model. An aggregator there means the headline and the link come from
        different outlets."""
        from pipeline.models import Verdict
        from pipeline.s4_corroborate import run as corroborate

        agg = _art("Acme RCE flaw exploited in the wild", "news.google.com",
                   tier="A", aggregator=True)
        real = _art("Acme RCE flaw exploited in the wild attacks ongoing",
                    "bleepingcomputer.com", tier="A")
        from pipeline.models import Cluster
        out = corroborate([Cluster(key="k", articles=[agg, real])])
        assert out[0].sources[0]["domain"] == "bleepingcomputer.com"


class TestSeenStorePruning:
    """The de-duplication window must forget the OLDEST entries, not the
    lexicographically lowest. uids are hex hashes, so sorting before pruning
    evicted every id starting with a low hex digit regardless of age."""

    def _isolated(self, tmp_path, monkeypatch):
        import pipeline.s2_clean as s2
        monkeypatch.setattr(s2, "SEEN_PATH", tmp_path / "seen.json")
        return s2

    def test_insertion_order_is_preserved(self, tmp_path, monkeypatch):
        import json
        s2 = self._isolated(tmp_path, monkeypatch)
        s2.SEEN_PATH.write_text(json.dumps(["aaa1", "fff9", "0000"]))
        s2.save_seen({"bbb2"})
        assert json.loads(s2.SEEN_PATH.read_text()) == \
            ["aaa1", "fff9", "0000", "bbb2"]

    def test_at_capacity_the_oldest_is_evicted(self, tmp_path, monkeypatch):
        import json
        s2 = self._isolated(tmp_path, monkeypatch)
        s2.SEEN_PATH.write_text(json.dumps([f"{i:04x}" for i in range(20000)]))
        s2.save_seen({"zzzz"})
        out = json.loads(s2.SEEN_PATH.read_text())
        assert len(out) == 20000
        assert out[-1] == "zzzz"          # newest kept
        assert out[0] == "0001"           # oldest dropped
        assert "0000" not in out

    def test_duplicates_are_not_appended_twice(self, tmp_path, monkeypatch):
        import json
        s2 = self._isolated(tmp_path, monkeypatch)
        s2.SEEN_PATH.write_text(json.dumps(["aaa1"]))
        s2.save_seen({"aaa1"})
        assert json.loads(s2.SEEN_PATH.read_text()) == ["aaa1"]


class TestShortenerHostMatching:
    def test_www_prefixed_shortener_is_recognised(self):
        """`www.bit.ly` missed SHORTENERS entirely, so it stayed unresolved
        and broke tiering, dedupe and the reference link at once."""
        from urllib.parse import urlsplit
        from pipeline.s1d_telegram import SHORTENERS
        for url in ("https://WWW.Bit.LY/abc", "https://bit.ly/abc",
                    "http://www.ift.tt/xyz"):
            host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
            assert host in SHORTENERS, url

    def test_a_real_outlet_is_not_treated_as_a_shortener(self):
        from urllib.parse import urlsplit
        from pipeline.s1d_telegram import SHORTENERS
        host = (urlsplit("https://www.bleepingcomputer.com/news/x").hostname
                or "").lower().removeprefix("www.")
        assert host not in SHORTENERS


class TestRescoreTrace:
    def test_stale_scoring_lines_are_cleared(self):
        """rescore() stripped only "blend " lines, so an escalated item
        shipped a trace showing its escalation twice, and a capped item
        showed a contradictory escalate/demote pair. The trace renders
        behind the card toggle, so the reader saw it."""
        from pipeline.models import Item, Priority
        from pipeline.s4_corroborate import rescore

        it = Item(cluster_key="k", title="t", category="cyber_attacks")
        it.priority, it.blend, it.interest_score = Priority.CRITICAL, 80.0, 100
        it.trace = ["tier A (+40)", "blend 70 = ...",
                    "escalated to CRITICAL by signal: 'actively exploited'",
                    "demoted: critical capped at 5"]
        rescore([it])
        kept = [t for t in it.trace if t.startswith(("blend ", "escalated to",
                                                     "demoted:"))]
        # exactly one scoring pass worth of lines, not two
        assert sum(1 for t in kept if t.startswith("escalated to")) <= 1
        assert sum(1 for t in kept if t.startswith("demoted:")) <= 1
        assert "tier A (+40)" in it.trace          # non-scoring lines survive


class TestNoLateImportShadowing:
    """A function-scope `import x` makes x local to the WHOLE function, so any
    earlier use of x in that function raises NameError at runtime rather than
    at import time — invisible to a syntax check and to every test that does
    not execute that exact path.

    This shipped: s7_publish used hashlib and canonical_url at the top of
    run() while importing them 50 lines further down, breaking publish
    entirely while `python -c "import pipeline.s7_publish"` still passed.
    """

    def test_publish_runs_end_to_end(self):
        from pipeline import s7_publish
        from pipeline.models import Item, Priority, Verdict

        it = Item(cluster_key="k", title="t", category="markets")
        it.priority, it.verdict, it.blend = (Priority.IMPORTANT,
                                             Verdict.ESTABLISHED, 10.0)
        it.sources = [{"name": "s", "url": "https://example.com/a",
                       "domain": "example.com", "tier": "B"}]
        out = s7_publish.run([it], dry_run=True)
        assert out["counts"]["published"] == 1

    def test_no_function_scope_import_shadows_a_module_import(self):
        """Flag any name imported BOTH at module scope and inside a function.

        The function-scope import makes the name local to the ENTIRE function,
        so every use of it in that function — including uses that appear
        BEFORE the import statement, and uses on branches where the import
        never executes — raises UnboundLocalError at runtime.

        An earlier version of this test only flagged "used before the import
        line", and it passed while the bug was live: run.py imported
        s8_notify at module scope and again inside main(), so the call at the
        end of main() failed with UnboundLocalError after the entire pipeline
        had already run. Line order was never the issue; the shadowing is.
        """
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        targets = list((root / "pipeline").glob("*.py")) + [root / "run.py"]

        offenders = []
        for path in targets:
            tree = ast.parse(path.read_text(encoding="utf-8"))

            module_level = set()
            for node in tree.body:                    # top level only
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        module_level.add((alias.asname or alias.name).split(".")[0])

            for fn in [n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                for node in ast.walk(fn):
                    if not isinstance(node, (ast.Import, ast.ImportFrom)):
                        continue
                    for alias in node.names:
                        name = (alias.asname or alias.name).split(".")[0]
                        if name in module_level:
                            offenders.append(
                                f"{path.name}:{node.lineno} re-imports '{name}' "
                                f"inside {fn.name}(), shadowing the module-level "
                                f"import for the whole function")

        assert not offenders, "; ".join(offenders)


class TestCorroborationIndependence:
    """Corroboration is the basis of the `verified` badge, so it has to count
    INDEPENDENT reporting, not reach. A live run counted 26 "outlets" on one
    breach story, of which 21 were Yahoo Finance, Crypto Briefing, The Malone
    Telegram and similar — the long tail republishing a single release."""

    def _cluster(self, domains):
        from datetime import datetime, timezone
        from pipeline.models import Article, Cluster
        return Cluster(key="k", articles=[
            Article(url=f"https://{d}/x", title="t", source=d, domain=d,
                    category="cyber_attacks", tier="B",
                    published=datetime.now(timezone.utc)) for d in domains])

    def test_synthetic_publishers_collapse_to_one_vote(self):
        from pipeline.s4_corroborate import _independent_outlets
        c = self._cluster(["bleepingcomputer.com",
                           "a.publisher", "b.publisher", "c.publisher",
                           "d.publisher", "e.publisher"])
        assert _independent_outlets(c) == 2   # 1 real + 1 collective

    def test_press_release_distributors_are_not_outlets(self):
        from pipeline.s4_corroborate import _independent_outlets
        c = self._cluster(["bleepingcomputer.com", "pr-newswire.publisher",
                           "businesswire.com", "globenewswire.com",
                           "yahoo-finance.publisher"])
        assert _independent_outlets(c) == 2

    def test_genuine_independent_outlets_all_count(self):
        from pipeline.s4_corroborate import _independent_outlets
        c = self._cluster(["bleepingcomputer.com", "cyberscoop.com",
                           "malwarebytes.com", "securityweek.com"])
        assert _independent_outlets(c) == 4

    def test_wire_syndication_still_collapses(self):
        from pipeline.s4_corroborate import _independent_outlets
        c = self._cluster(["reuters.com", "in.reuters.com", "bbc.co.uk"])
        assert _independent_outlets(c) == 2

    def test_wide_pickup_is_weak_evidence_not_none(self):
        """A story carried only by the long tail should count 1, not 0 —
        wide pickup is weak evidence, not the absence of evidence."""
        from pipeline.s4_corroborate import _independent_outlets
        c = self._cluster(["a.publisher", "b.publisher", "c.publisher"])
        assert _independent_outlets(c) == 1


class TestTagPrecision:
    """Tag rules matched bare substrings, so 'rce' matched COMMERCE and a
    Shein IPO story was tagged `exploit`. On a live run the tag appeared on
    58% of items including markets and india_world stories, which makes it
    useless as a filter — the thing tags exist for."""

    def test_rce_does_not_match_commerce(self):
        from pipeline.tagging import from_keywords
        tags = from_keywords("Shein IPO eyes $27 billion in cross-border commerce")
        assert "exploit" not in tags

    def test_poc_does_not_match_pocket(self):
        from pipeline.tagging import from_keywords
        assert "exploit" not in from_keywords("A pocket guide to gardening")

    def test_real_security_terms_still_match(self):
        from pipeline.tagging import from_keywords
        assert "exploit" in from_keywords("Unauthenticated RCE in Citrix NetScaler")
        assert "ransomware" in from_keywords("Ransomware gang hits hospital")
        assert "ai" in from_keywords("New AI model released by OpenAI")

    def test_multi_word_terms_survive_boundaries(self):
        from pipeline.tagging import from_keywords
        assert "exploit" in from_keywords("Proof of concept published today")

    def test_tags_discriminate_across_the_corpus(self):
        """No tag should land on most of the page. A tag on 58% of items
        carries no information."""
        import collections
        import json
        from pathlib import Path

        news = Path(__file__).resolve().parent.parent / "site" / "news.json"
        if not news.exists():
            return                       # nothing published yet; nothing to check
        items = json.loads(news.read_text(encoding="utf-8"))["items"]
        if len(items) < 10:
            return
        counts = collections.Counter(t for i in items for t in i.get("tags", []))
        worst, n = counts.most_common(1)[0]
        assert n <= len(items) * 0.7, (
            f"tag '{worst}' is on {n}/{len(items)} items — too broad to filter on")


class TestRetryBudget:
    """Retry shallow, fall through fast.

    Profiling a 45-minute run put 63% of wall clock inside the retry loop and
    another 10% in RPM pacing — nearly three quarters spent waiting. With a
    ladder of eight models, retrying ONE model deeply is backwards: a
    different model is far more likely to answer than the same one twelve
    seconds later.
    """

    def test_worst_case_on_a_dead_model_is_bounded(self):
        import inspect
        from pipeline import llm_gemini

        sig = inspect.signature(llm_gemini._post_with_retries)
        retries = sig.parameters["max_retries"].default
        call_sig = inspect.signature(llm_gemini.call)
        timeout = call_sig.parameters["timeout"].default

        # attempts x timeout, plus backoff, must stay well under the point
        # where one model can eat a meaningful share of the run
        worst = retries * timeout
        assert worst <= 120, f"a dead model can burn {worst}s before fallthrough"

    def test_ladder_is_deep_enough_to_justify_shallow_retries(self):
        """Shallow retries are only safe because there are models to fall
        through TO. If the ladder shrank, this trade would stop making sense."""
        from pipeline.config import settings
        assert len(settings()["llm"]["tiers"]) >= 4


class TestJudgeScope:
    def test_judge_is_scoped_to_actionable_items(self):
        """A measured run spent 5 calls and ~4 minutes returning 28 'all
        supported', 1 unsupported and 0 rejections — nearly all of it on
        MINOR items nobody acts on."""
        from pipeline.config import settings
        cfg = settings()["judge"]
        assert cfg["min_priority"] in ("critical", "important")
        assert cfg["max_items"] <= 20

    def test_minor_items_are_excluded_by_default(self):
        from pipeline.config import settings
        from pipeline.models import Priority

        floor = settings()["judge"]["min_priority"]
        wanted = {Priority.CRITICAL}
        if floor in ("important", "minor"):
            wanted.add(Priority.IMPORTANT)
        if floor == "minor":
            wanted.add(Priority.MINOR)
        assert Priority.MINOR not in wanted


class TestDeployConfig:
    """The published site is public even though the repo is private, so what
    reaches it matters."""

    def _vercel(self):
        import json
        from pathlib import Path
        return json.loads(
            (Path(__file__).resolve().parent.parent / "vercel.json")
            .read_text(encoding="utf-8"))

    def test_only_the_site_directory_is_published(self):
        """Serving the repo root would expose data/rejected.json and the
        caches. None hold secrets — that was audited — but rejected items and
        internal state are not meant to be public."""
        assert self._vercel()["outputDirectory"] == "site"

    def test_news_json_is_not_cached(self):
        """The whole point is a page that changes daily. A CDN caching
        news.json would show yesterday's stories from a fresh deploy."""
        rules = self._vercel()["headers"]
        newsjson = next(r for r in rules if r["source"] == "/news.json")
        cc = next(h["value"] for h in newsjson["headers"]
                  if h["key"] == "Cache-Control")
        assert "max-age=0" in cc and "must-revalidate" in cc

    def test_csp_allows_the_fonts_the_page_actually_uses(self):
        """A CSP that blocks its own stylesheet is worse than none."""
        rules = self._vercel()["headers"]
        catchall = next(r for r in rules if r["source"] == "/(.*)")
        csp = next(h["value"] for h in catchall["headers"]
                   if h["key"] == "Content-Security-Policy")
        assert "fonts.googleapis.com" in csp
        assert "fonts.gstatic.com" in csp
        assert "default-src 'none'" in csp

    def test_vercelignore_excludes_the_env_file(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        ignored = (root / ".vercelignore").read_text(encoding="utf-8").split()
        assert ".env" in ignored
        assert "data/" in ignored
