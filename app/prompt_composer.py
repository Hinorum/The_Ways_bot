"""Prompt Composition Language — декларативная сборка промптов.

Архитектурный инсайт: сегодня промпт собирается через строковую
конкатенацию в _build_story_prompt(). Это хрупко: порядок секций
захардкожен, условия晦涩, и добавление новой секции требует
изменения основного pipeline.

DSL позволяет описывать промпт декларативно:

    prompt = PromptBuilder()
    prompt.section("world", source="lore", priority=1)
    prompt.section("npc", source="relations", min_sentiment=-1)
    prompt.section("memory", source="memory", max_chapters=3)
    prompt.section("echoes", source="echoes", condition="surfaced_today")
    result = await prompt.build(session)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


@dataclass
class PromptSection:
    """Одна секция промпта."""

    name: str
    source: str  # модуль-источник: "lore", "relations", "memory", etc.
    priority: int = 50  # порядок (меньше = раньше)
    enabled: bool = True
    condition: str | None = None  # условие включения: "surfaced_today", "ton_enabled", etc.
    params: dict[str, Any] = field(default_factory=dict)  # доп. параметры

    # Вычисляемое поле: готовый текст секции
    _text: str | None = field(default=None, repr=False)

    def set_text(self, text: str | None) -> None:
        self._text = text

    @property
    def text(self) -> str | None:
        return self._text

    @property
    def is_empty(self) -> bool:
        return self._text is None or not self._text.strip()


class PromptBuilder:
    """Декларативный сборщик промптов.

    Использование::

        builder = PromptBuilder()
        builder.add_section("world_bible", "lore", priority=1)
        builder.add_section("npc_relations", "relations", priority=10)
        builder.add_section("callings", "callings", priority=15)
        builder.add_section("memory", "memory", priority=30, max_chapters=3)
        builder.add_section("echoes", "echoes", priority=40, condition="surfaced")
        builder.add_section("season", "season", priority=5)
        builder.add_section("arc", "story_arc", priority=12)
        builder.add_section("villain", "villain", priority=20, condition="villain_in_guests")

        result = await builder.build(session)
    """

    def __init__(self) -> None:
        self._sections: list[PromptSection] = []

    def add_section(
        self,
        name: str,
        source: str,
        priority: int = 50,
        condition: str | None = None,
        **params: Any,
    ) -> PromptBuilder:
        """Добавляет секцию в промпт."""
        section = PromptSection(
            name=name,
            source=source,
            priority=priority,
            condition=condition,
            params=params,
        )
        self._sections.append(section)
        return self

    def _check_condition(
        self,
        condition: str | None,
        context: dict[str, Any],
    ) -> bool:
        """Проверяет условие включения секции."""
        if condition is None:
            return True

        # Простые условия
        if condition == "ton_enabled":
            return context.get("ton_enabled", False)
        if condition == "surfaced_today":
            return context.get("has_surfaced_echoes", False)
        if condition == "villain_in_guests":
            return "villain" in (context.get("guests") or [])
        if condition == "heretic_in_guests":
            return "heretic" in (context.get("guests") or [])
        if condition == "sealed_day":
            return context.get("is_sealed", False)
        if condition == "has_callings":
            return bool(context.get("active_callings"))
        if condition == "has_npc_relations":
            return bool(context.get("npc_relations"))
        if condition == "has_echoes":
            return bool(context.get("echoes"))
        if condition == "has_distant_memory":
            return bool(context.get("distant_memory"))

        # Неизвестные условия разрешаем (секция включена)
        logger.debug("Неизвестное условие: %s", condition)
        return True

    async def build(
        self,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Собирает промпт из секций.

        context — словарь с данными для проверки условий и передачи
        параметров в source-функции.
        """
        ctx = context or {}

        # Фильтруем и сортируем по priority
        active = [
            s
            for s in self._sections
            if s.enabled and self._check_condition(s.condition, ctx)
        ]
        active.sort(key=lambda s: s.priority)

        # Собираем тексты
        parts = []
        for section in active:
            if section.is_empty:
                continue
            text = section.text.strip()
            if text:
                parts.append(text)

        return "\n\n".join(parts)

    def get_sections(self) -> list[PromptSection]:
        return list(self._sections)


# ── Source functions: функции-источники для секций ──

async def source_lore(ctx: dict[str, Any]) -> str | None:
    """Источник: мир и лор."""
    return ctx.get("season_block")


async def source_relations(ctx: dict[str, Any]) -> str | None:
    """Источник: отношения NPC."""
    return ctx.get("relations_block")


async def source_callings(ctx: dict[str, Any]) -> str | None:
    """Источник: призвания стаи."""
    return ctx.get("callings_block")


async def source_memory(ctx: dict[str, Any]) -> str | None:
    """Источник: дальняя память."""
    distant = ctx.get("distant_memory", [])
    if not distant:
        return None
    lines = ["Давний канон (память мира):"]
    for beat in distant[:ctx.get("max_chapters", 3)]:
        lines.append(f"  — {beat}")
    return "\n".join(lines)


async def source_echoes(ctx: dict[str, Any]) -> str | None:
    """Источник: эхо-следы."""
    echoes = ctx.get("echoes", [])
    if not echoes:
        return None
    lines = ["Эхо-следы:"]
    for echo in echoes[:5]:
        lines.append(f"  — {echo.title}")
    return "\n".join(lines)


async def source_season(ctx: dict[str, Any]) -> str | None:
    """Источник: сезонная рамка."""
    return ctx.get("season_block")


async def source_arc(ctx: dict[str, Any]) -> str | None:
    """Источник: арка месяца."""
    return ctx.get("arc_block")


async def source_villain(ctx: dict[str, Any]) -> str | None:
    """Источник: план злодея."""
    return ctx.get("villain_block")


async def source_heretic(ctx: dict[str, Any]) -> str | None:
    """Источник: правила еретика."""
    return ctx.get("heretic_block")


async def source_alignment(ctx: dict[str, Any]) -> str | None:
    """Источник: нрав стаи."""
    return ctx.get("alignment_block")


async def source_focus(ctx: dict[str, Any]) -> str | None:
    """Источник: фокус NPC."""
    return ctx.get("focus_line")


async def source_repeat(ctx: dict[str, Any]) -> str | None:
    """Источник: банк повторов."""
    return ctx.get("repeat_block")


# ── Реестр source functions ──

SOURCE_REGISTRY: dict[str, Callable[..., Coroutine[Any, Any, str | None]]] = {
    "lore": source_lore,
    "relations": source_relations,
    "callings": source_callings,
    "memory": source_memory,
    "echoes": source_echoes,
    "season": source_season,
    "story_arc": source_arc,
    "villain": source_villain,
    "heretic": source_heretic,
    "alignment": source_alignment,
    "focus": source_focus,
    "repeat": source_repeat,
}


class PromptComposer:
    """Высокоуровневый композитор: связывает DSL с source functions.

    Использование::

        composer = PromptComposer()
        composer.add_section("world", "lore", priority=1)
        composer.add_section("npc", "relations", priority=10)

        # Регистрируем кастомный source
        composer.register_source("my_custom", my_custom_source)

        result = await composer.compose(session, context)
    """

    def __init__(self) -> None:
        self._builder = PromptBuilder()
        self._sources: dict[str, Callable[..., Coroutine[Any, Any, str | None]]] = dict(
            SOURCE_REGISTRY
        )

    def register_source(
        self,
        name: str,
        func: Callable[..., Coroutine[Any, Any, str | None]],
    ) -> None:
        """Регистрирует кастомный source."""
        self._sources[name] = func

    def add_section(
        self,
        name: str,
        source: str,
        priority: int = 50,
        condition: str | None = None,
        **params: Any,
    ) -> PromptComposer:
        self._builder.add_section(name, source, priority, condition, **params)
        return self

    async def compose(
        self,
        session: Any = None,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Композит промпт: заполняет секции из sources и собирает."""
        ctx = context or {}

        for section in self._builder.get_sections():
            source_func = self._sources.get(section.source)
            if source_func is None:
                logger.warning("Source не найден: %s", section.source)
                continue
            try:
                text = await source_func(ctx)
                section.set_text(text)
            except Exception:
                logger.exception("Source %s не удался для секции %s", section.source, section.name)

        return await self._builder.build(ctx)
