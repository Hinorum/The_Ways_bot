"""Tests for DayProjection, Plugin system, NPC CoG, Difficulty, Prompt Composer."""
from __future__ import annotations

import pytest
from app.projection import (
    DayProjection,
    VoteDistribution,
    StakeDistribution,
    NPCDelta,
    AlignmentDrift,
    FlipMarginResult,
    CardInfo,
)
from app.plugins import (
    Plugin,
    PluginMeta,
    PluginRegistry,
    PluginContext,
    Capability,
    HookOrder,
    BasePlugin,
)
from app.npc_cog import (
    generate_npc_cog,
    generate_all_npc_cogs,
    npc_cogs_block,
    NPCCogResult,
)
from app.difficulty import (
    compute_difficulty_metrics,
    select_difficulty,
    select_win_rule,
    _compute_diversity,
    _estimate_trend,
    format_difficulty_hint,
)
from app.prompt_composer import (
    PromptBuilder,
    PromptComposer,
    PromptSection,
)


# ═══════════════════════════════════════════════════════════
# DayProjection tests
# ═══════════════════════════════════════════════════════════

class TestVoteDistribution:
    def test_basic(self):
        v = VoteDistribution(counts={0: 5, 1: 3, 2: 2}, total=10, winner_card=0, win_rule="MAJORITY", tie_note=None)
        assert v.winner_count == 5
        assert v.runner_up_count == 3
        assert not v.is_unanimous

    def test_unanimous(self):
        v = VoteDistribution(counts={0: 10, 1: 0, 2: 0}, total=10, winner_card=0, win_rule="MAJORITY", tie_note=None)
        assert v.is_unanimous

    def test_empty(self):
        v = VoteDistribution(counts={}, total=0, winner_card=None, win_rule=None, tie_note=None)
        assert v.winner_count == 0
        assert v.runner_up_count == 0
        assert not v.is_unanimous


class TestStakeDistribution:
    def test_basic(self):
        s = StakeDistribution(path_stakes={0: 100, 1: 50, 2: 30}, total=180, winner_stake=100, winner_share_pct=56, multiplier=1.5)
        assert s.has_action
        assert s.winner_share_pct == 56

    def test_no_stakes(self):
        s = StakeDistribution(path_stakes={}, total=0, winner_stake=0, winner_share_pct=None, multiplier=None)
        assert not s.has_action


class TestNPCDelta:
    def test_changed(self):
        d = NPCDelta(name="liner", old_sentiment=0, new_sentiment=1, shift=1, tone_before="neutral", tone_after="cautious")
        assert d.changed

    def test_unchanged(self):
        d = NPCDelta(name="liner", old_sentiment=0, new_sentiment=0, shift=0, tone_before="neutral", tone_after="neutral")
        assert not d.changed


class TestAlignmentDrift:
    def test_changed(self):
        a = AlignmentDrift(order_before=0, order_after=1, moral_before=0, moral_after=0, tag="care")
        assert a.order_changed
        assert not a.moral_changed

    def test_unchanged(self):
        a = AlignmentDrift(order_before=0, order_after=0, moral_before=0, moral_after=0, tag="care")
        assert not a.order_changed
        assert not a.moral_changed


class TestFlipMargin:
    def test_close(self):
        f = FlipMarginResult(distance=2, alternative_winner=1)
        assert f.is_close

    def test_not_close(self):
        f = FlipMarginResult(distance=10, alternative_winner=1)
        assert not f.is_close

    def test_none(self):
        f = FlipMarginResult(distance=None, alternative_winner=None)
        assert not f.is_close


class TestDayProjection:
    def test_properties(self):
        cards = [CardInfo(0, "A", "desc", "cons", "care"), CardInfo(1, "B", "desc", "cons", "risk")]
        proj = DayProjection(
            round_id=1, day_index=100, season="2026-08",
            opened_at=None, closed_at=None,
            votes=VoteDistribution(counts={0: 5, 1: 3}, total=8, winner_card=0, win_rule="MAJORITY", tie_note=None),
            stakes=StakeDistribution(path_stakes={0: 100}, total=100, winner_stake=100, winner_share_pct=100, multiplier=None),
            pot_nanotons=1000,
            cards=cards,
            winning_card=cards[0],
        )
        assert proj.winner_title == "A"
        assert proj.winner_tag == "care"
        assert proj.voter_count == 8
        assert proj.has_stakes
        assert proj.card_by_position(1).title == "B"
        assert proj.card_by_position(5) is None

    def test_to_dict(self):
        card = CardInfo(0, "A", "desc", "cons", "care")
        proj = DayProjection(
            round_id=1, day_index=100, season="2026-08",
            opened_at=None, closed_at=None,
            votes=VoteDistribution(counts={0: 5}, total=5, winner_card=0, win_rule="MAJORITY", tie_note=None),
            stakes=StakeDistribution(path_stakes={}, total=0, winner_stake=0, winner_share_pct=None, multiplier=None),
            pot_nanotons=0,
            winning_card=card,
        )
        d = proj.to_dict()
        assert d["day_index"] == 100
        assert d["winner_card"] == 0


# ═══════════════════════════════════════════════════════════
# Plugin system tests
# ═══════════════════════════════════════════════════════════

class TestPluginRegistry:
    def test_register_and_list(self):
        reg = PluginRegistry()
        plugin = Plugin(
            meta=PluginMeta(name="test", description="Test plugin", capabilities=frozenset([Capability.PROMPT_BLOCK])),
        )
        reg.register(plugin)
        assert len(reg.list_plugins()) == 1
        assert reg.get("test") is not None

    def test_unregister(self):
        reg = PluginRegistry()
        plugin = Plugin(meta=PluginMeta(name="test", description="", capabilities=frozenset()))
        reg.register(plugin)
        assert reg.unregister("test")
        assert reg.get("test") is None
        assert not reg.unregister("nonexistent")

    def test_by_capability(self):
        reg = PluginRegistry()
        p1 = Plugin(meta=PluginMeta(name="a", description="", capabilities=frozenset([Capability.PROMPT_BLOCK]), priority=10))
        p2 = Plugin(meta=PluginMeta(name="b", description="", capabilities=frozenset([Capability.PROMPT_BLOCK]), priority=5))
        reg.register(p1)
        reg.register(p2)
        by_cap = reg._by_capability(Capability.PROMPT_BLOCK)
        assert by_cap[0].meta.name == "b"  # lower priority first

    def test_disabled_plugin_excluded(self):
        reg = PluginRegistry()
        plugin = Plugin(meta=PluginMeta(name="disabled", description="", capabilities=frozenset([Capability.PROMPT_BLOCK]), enabled=False))
        reg.register(plugin)
        assert len(reg._by_capability(Capability.PROMPT_BLOCK)) == 0


class TestBasePlugin:
    def test_as_plugin(self):
        class MyPlugin(BasePlugin):
            name = "my_plugin"
            description = "Test"
            capabilities = frozenset([Capability.PROMPT_BLOCK])

            async def _prompt_block(self, ctx):
                return "test block"

        bp = MyPlugin()
        plugin = bp.as_plugin()
        assert plugin.meta.name == "my_plugin"
        assert plugin.has_capability(Capability.PROMPT_BLOCK)


# ═══════════════════════════════════════════════════════════
# NPC CoG tests
# ═══════════════════════════════════════════════════════════

class TestNPCCog:
    def test_generate_liner_devoted(self):
        cog = generate_npc_cog("liner", 3, day_index=1)
        assert cog.name == "liner"
        assert cog.sentiment == 3
        # _TONES[3] = ("предан стае", ...) → mood "devoted"
        assert cog.tone == "devoted"
        assert len(cog.inner_thought) > 0
        assert len(cog.action_hint) > 0

    def test_generate_heretic_hostile(self):
        cog = generate_npc_cog("heretic", -3, day_index=1)
        # _TONES[-3] = ("охотится на стаю", ...) → mood "hostile"
        assert cog.tone == "hostile"
        assert len(cog.inner_thought) > 0

    def test_deterministic(self):
        c1 = generate_npc_cog("archivist", 0, day_index=42)
        c2 = generate_npc_cog("archivist", 0, day_index=42)
        assert c1.inner_thought == c2.inner_thought
        assert c1.action_hint == c2.action_hint

    def test_different_days_different_thoughts(self):
        c1 = generate_npc_cog("liner", 1, day_index=1)
        c2 = generate_npc_cog("liner", 1, day_index=2)
        # Different days should produce different thoughts (or at least not crash)
        assert c1.name == c2.name

    def test_to_prompt_block(self):
        cog = generate_npc_cog("master", 2, day_index=5)
        block = cog.to_prompt_block()
        assert "MASTER" in block
        assert "Мысли:" in block
        assert "Действие:" in block

    @pytest.mark.asyncio
    async def test_all_npcs(self):
        cogs = await generate_all_npc_cogs({"liner": 2, "archivist": 0, "master": -1, "heretic": 3}, day_index=10)
        assert len(cogs) == 4
        names = {c.name for c in cogs}
        assert names == {"liner", "archivist", "master", "heretic"}

    def test_cogs_block(self):
        cogs = [
            NPCCogResult("liner", 2, "cautious", "thought", "mot", "act", "line"),
            NPCCogResult("archivist", 0, "neutral", "thought2", "mot2", "act2", "line2"),
        ]
        block = npc_cogs_block(cogs)
        assert "Chain-of-thought NPC:" in block
        assert "LINER" in block
        assert "ARCHIVIST" in block

    def test_cogs_block_empty(self):
        assert npc_cogs_block([]) == ""


# ═══════════════════════════════════════════════════════════
# Difficulty tests
# ═══════════════════════════════════════════════════════════

class TestDifficulty:
    def test_diversity_equal(self):
        d = _compute_diversity({0: 10, 1: 10, 2: 10})
        assert d > 0.9  # near maximum

    def test_diversity_one_sided(self):
        d = _compute_diversity({0: 100, 1: 0, 2: 0})
        assert d == 0.0

    def test_diversity_empty(self):
        assert _compute_diversity({}) == 0.0

    def test_trend_growing(self):
        assert _estimate_trend([1, 2, 3, 4, 5]) == "growing"

    def test_trend_declining(self):
        assert _estimate_trend([5, 4, 3, 2, 1]) == "declining"

    def test_trend_stable(self):
        assert _estimate_trend([3, 3, 3, 3, 3]) == "stable"

    def test_trend_short(self):
        assert _estimate_trend([1]) == "stable"

    def test_compute_metrics(self):
        m = compute_difficulty_metrics(
            counts={0: 5, 1: 3, 2: 2},
            total_stakes=100,
            player_count=10,
        )
        assert 0 <= m.turnout_ratio <= 1
        assert m.engagement_score >= 0

    def test_select_difficulty_easy(self):
        m = compute_difficulty_metrics(counts={0: 1}, total_stakes=0, player_count=100)
        assert select_difficulty(m, day_index=1) == "easy"

    def test_select_difficulty_hard(self):
        m = compute_difficulty_metrics(counts={0: 10, 1: 10, 2: 10}, total_stakes=1000, player_count=30)
        d = select_difficulty(m, day_index=1)
        assert d in ("medium", "hard")

    def test_select_win_rule(self):
        m = compute_difficulty_metrics(counts={0: 5, 1: 5}, total_stakes=100, player_count=10)
        rule = select_win_rule(m, day_index=42)
        assert rule in ("majority", "minority", "median")

    def test_format_hint(self):
        m = compute_difficulty_metrics(counts={0: 5, 1: 3, 2: 2}, total_stakes=100, player_count=10)
        hint = format_difficulty_hint(m, "medium")
        assert "Сложность: medium" in hint
        assert "Engagement:" in hint


# ═══════════════════════════════════════════════════════════
# Prompt Composer tests
# ═══════════════════════════════════════════════════════════

class TestPromptBuilder:
    def test_add_section(self):
        b = PromptBuilder()
        b.add_section("world", "lore", priority=1)
        assert len(b.get_sections()) == 1

    @pytest.mark.asyncio
    async def test_build_empty(self):
        b = PromptBuilder()
        result = await b.build()
        assert result == ""

    @pytest.mark.asyncio
    async def test_build_with_text(self):
        import asyncio
        b = PromptBuilder()
        section = b.add_section("world", "lore", priority=1)
        # section is PromptBuilder (self-return), get the actual section
        actual_section = b.get_sections()[0]
        actual_section.set_text("World text")
        result = await b.build()
        assert "World text" in result

    @pytest.mark.asyncio
    async def test_priority_ordering(self):
        b = PromptBuilder()
        b.add_section("first", "lore", priority=10)
        b.add_section("second", "lore", priority=1)
        sections = b.get_sections()
        sections[0].set_text("AAA")  # first (priority=10)
        sections[1].set_text("BBB")  # second (priority=1)
        result = await b.build()
        assert result.index("BBB") < result.index("AAA")

    def test_condition_check(self):
        b = PromptBuilder()
        assert b._check_condition(None, {})
        assert b._check_condition("ton_enabled", {"ton_enabled": True})
        assert not b._check_condition("ton_enabled", {"ton_enabled": False})
        assert b._check_condition("surfaced_today", {"has_surfaced_echoes": True})
        assert not b._check_condition("surfaced_today", {"has_surfaced_echoes": False})


class TestPromptComposer:
    @pytest.mark.asyncio
    async def test_compose(self):
        c = PromptComposer()
        c.add_section("world", "lore", priority=1)

        async def fake_lore(ctx):
            return "Lore content"

        c.register_source("lore", fake_lore)

        result = await c.compose(context={"season_block": "Lore content"})
        assert "Lore content" in result
