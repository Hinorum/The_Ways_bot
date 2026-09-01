"""Built-in plugins — обёртки существующих механик над plugin protocol.

Каждый плагин инкапсулирует одну механику и декларирует свои capabilities.
При вызове hooks плагин читает DayProjection вместо прямых запросов к БД.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.plugins import (
    BasePlugin,
    Capability,
    HookOrder,
    PluginContext,
    registry,
)

if TYPE_CHECKING:
    from app.projection import DayProjection

logger = logging.getLogger(__name__)


# ── Echoes Plugin ──

class EchoesPlugin(BasePlugin):
    """Плагин эхо-следов: триггерит и управляет цепочками эхо."""

    name = "echoes"
    description = "Эхо-следы прошлых выборов: всплытие, цепочки, квизы"
    capabilities = frozenset([Capability.POST_DAY_HOOK, Capability.PROMPT_BLOCK])

    async def _post_day_hook(self, ctx: PluginContext) -> None:
        """Эхо уже рождены в close_voting. Здесь — логирование."""
        proj = ctx.projection
        if proj is None:
            return
        logger.debug(
            "Echoes: день %d, %d голосов, тег победителя: %s",
            proj.day_index,
            proj.voter_count,
            proj.winner_tag,
        )

    async def _prompt_block(self, ctx: PluginContext) -> str | None:
        """Блок эхо для промпта — собирается из DayProjection."""
        proj = ctx.projection
        if proj is None or not proj.echoed_echoes:
            return None
        lines = [f"Эхо-следы дня {proj.day_index}:"]
        lines.append(f"  Всплывших эхо: {len(proj.echoed_echoes)}")
        return "\n".join(lines)


# ── Relations Plugin ──

class RelationsPlugin(BasePlugin):
    """Плагин отношений NPC: отслеживание дельт и тонов."""

    name = "relations"
    description = "Отношения NPC к стае: тон, дельты, фокусные линии"
    capabilities = frozenset([Capability.POST_DAY_HOOK, Capability.PROMPT_BLOCK])

    async def _post_day_hook(self, ctx: PluginContext) -> None:
        proj = ctx.projection
        if proj is None:
            return
        for delta in proj.npc_deltas:
            if delta.changed:
                logger.info(
                    "NPC %s: %+d (%s → %s)",
                    delta.name,
                    delta.shift,
                    delta.tone_before,
                    delta.tone_after,
                )

    async def _prompt_block(self, ctx: PluginContext) -> str | None:
        proj = ctx.projection
        if proj is None:
            return None
        changed = [d for d in proj.npc_deltas if d.changed]
        if not changed:
            return None
        lines = ["Отношения NPC изменились:"]
        for d in changed:
            lines.append(f"  {d.name}: {d.tone_before} → {d.tone_after} ({d.shift:+d})")
        return "\n".join(lines)


# ── Trail Plugin ──

class TrailPlugin(BasePlugin):
    """Плагин нрава стаи: вычисляемая идентичность и дрейф осей."""

    name = "trail"
    description = "Нрав стаи: порядок × мораль, эмерджентная идентичность"
    capabilities = frozenset([Capability.POST_DAY_HOOK, Capability.PROMPT_BLOCK])

    async def _post_day_hook(self, ctx: PluginContext) -> None:
        proj = ctx.projection
        if proj is None or proj.alignment is None:
            return
        a = proj.alignment
        if a.order_changed or a.moral_changed:
            logger.info(
                "Trail drift: order %d→%d, moral %d→%d (tag=%s)",
                a.order_before,
                a.order_after,
                a.moral_before,
                a.moral_after,
                a.tag,
            )

    async def _prompt_block(self, ctx: PluginContext) -> str | None:
        proj = ctx.projection
        if proj is None or proj.alignment is None:
            return None
        a = proj.alignment
        if not (a.order_changed or a.moral_changed):
            return None
        return (
            f"Нрав стаи дрейфнул: порядок {a.order_before}→{a.order_after}, "
            f"мораль {a.moral_before}→{a.moral_after}"
        )


# ── Economics Plugin ──

class EconomicsPlugin(BasePlugin):
    """Плагин экономики: ставки, множители, распределение банка."""

    name = "economics"
    description = "Экономика дня: банк, ставки, множитель, копилки"
    capabilities = frozenset([Capability.POST_DAY_HOOK, Capability.RESULTS_FORMAT])

    async def _post_day_hook(self, ctx: PluginContext) -> None:
        proj = ctx.projection
        if proj is None:
            return
        if proj.stakes.has_action:
            logger.info(
                "Economics: банк %d нанотонов, ставки %d, множитель %s",
                proj.pot_nanotons,
                proj.stakes.total,
                f"×{proj.stakes.multiplier:.2f}" if proj.stakes.multiplier else "—",
            )

    async def _results_format(self, ctx: PluginContext) -> str | None:
        """Экономический блок для поста итогов."""
        proj = ctx.projection
        if proj is None:
            return None
        from app.ton_utils import from_nano

        lines = []
        if proj.stakes.has_action:
            parts = " · ".join(
                f"{('I', 'II', 'III')[pos]} {from_nano(stake):.2f}"
                for pos, stake in sorted(proj.stakes.path_stakes.items())
            )
            lines.append(f"💸 Ставки на пути: {parts} Gram")
            if proj.stakes.multiplier:
                lines.append(f"🎯 Коэффициент: ×{proj.stakes.multiplier:.2f}")
        if proj.flip_margin and proj.flip_margin.is_close:
            k = proj.flip_margin.distance
            word = "голос" if k == 1 else "голоса" if k <= 4 else "голосов"
            lines.append(f"🩸 На волоске: ещё {k} {word} — и тропа повела бы иначе.")
        return "\n".join(lines) if lines else None


# ── Bestiary Plugin ──

class BestiaryPlugin(BasePlugin):
    """Плагин бестиария: фиксация наблюдений существ."""

    name = "bestiary"
    description = "Бестиарий лабиринта: наблюдения существ по сезонам"
    capabilities = frozenset([Capability.POST_DAY_HOOK])

    async def _post_day_hook(self, ctx: PluginContext) -> None:
        proj = ctx.projection
        if proj is None:
            return
        # Бестиарий уже записывается в note_round(). Здесь — логирование.
        logger.debug(
            "Bestiary: день %d, act_stage=%d, sealed=%s",
            proj.day_index,
            proj.act_stage,
            proj.is_sealed,
        )


# ── FlipMargin Plugin ──

class FlipMarginPlugin(BasePlugin):
    """Плагин «канон на волоске»: анализ минимального переворота."""

    name = "flip_margin"
    description = "Анализ переворота: сколько голосов отделяло от другого исхода"
    capabilities = frozenset([Capability.POST_DAY_HOOK, Capability.PROMPT_BLOCK])

    async def _post_day_hook(self, ctx: PluginContext) -> None:
        proj = ctx.projection
        if proj is None or proj.flip_margin is None:
            return
        fm = proj.flip_margin
        if fm.is_close:
            logger.info(
                "Flip margin: %d голосов до альтернативного исхода (path=%s)",
                fm.distance,
                fm.alternative_winner,
            )

    async def _prompt_block(self, ctx: PluginContext) -> str | None:
        proj = ctx.projection
        if proj is None or proj.flip_margin is None:
            return None
        fm = proj.flip_margin
        if fm.distance is None:
            return None
        return (
            f"Канон дня {proj.day_index}: {fm.distance} голосов "
            f"отделяло от альтернативного исхода"
        )


# ── Registration ──

_registered = False


def register_builtin_plugins() -> None:
    """Регистрирует все встроенные плагины в глобальный реестр (один раз)."""
    global _registered
    if _registered:
        return
    for plugin_cls in (
        EchoesPlugin,
        RelationsPlugin,
        TrailPlugin,
        EconomicsPlugin,
        BestiaryPlugin,
        FlipMarginPlugin,
    ):
        plugin = plugin_cls()
        plugin.register_self()
    _registered = True
    logger.info(
        "Зарегистрированы встроенные плагины: %s",
        ", ".join(p.name for p in [
            EchoesPlugin(), RelationsPlugin(), TrailPlugin(),
            EconomicsPlugin(), BestiaryPlugin(), FlipMarginPlugin(),
        ]),
    )
