"""Plugin capability system — регистрирует игровую механику как плагин.

Архитектурный инсайт: сегодня каждая механика (echoes, trail, callings,
bestiary) зашита в конкретный модуль. Плагинная система позволяет:
1. Декларировать capabilities (что плагин делает)
2. Регистрировать hooks (когда плагин активируется)
3. Инжектировать данные в промпт (через prompt_block)
4. Обрабатывать post-day проекцию (через post_day_hook)

Аналог Covel plugin architecture, адаптированный под ежедневный цикл.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Coroutine

if TYPE_CHECKING:
    from app.projection import DayProjection

logger = logging.getLogger(__name__)


class Capability(str, Enum):
    """Возможности, которые может декларировать плагин."""

    # Генерация блока для промпта Ведущего
    PROMPT_BLOCK = "prompt_block"

    # Обработка результата дня (post-tally hook)
    POST_DAY_HOOK = "post_day_hook"

    # Генерация текста для поста итогов
    RESULTS_FORMAT = "results_format"

    # Обработка эхо-триггеров
    ECHO_TRIGGER = "echo_trigger"

    # Личная лента игрока (personal echo coloring)
    PERSONAL_ECHO = "personal_echo"

    # Генерация данных для следующего дня (pre-day hook)
    PRE_DAY_HOOK = "pre_day_hook"

    # Валидация данных дня
    VALIDATOR = "validator"


class HookOrder(int, Enum):
    """Порядок выполнения hooks (меньше = раньше)."""

    EARLY = 10
    NORMAL = 50
    LATE = 90
    POST = 100  # после всех остальных


@dataclass
class PluginMeta:
    """Метаданные плагина."""

    name: str
    description: str
    version: str = "1.0.0"
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    priority: int = HookOrder.NORMAL
    enabled: bool = True


@dataclass
class PluginContext:
    """Контекст, передаваемый плагину при вызове hooks."""

    projection: DayProjection | None = None
    session: Any = None  # AsyncSession
    extra: dict[str, Any] = field(default_factory=dict)


# Типы callback-ов для hooks
PromptBlockGetter = Callable[[PluginContext], Coroutine[Any, Any, str | None]]
PostDayHook = Callable[[PluginContext], Coroutine[Any, Any, None]]
ResultsFormatter = Callable[[PluginContext], Coroutine[Any, Any, str | None]]
PreDayHook = Callable[[PluginContext], Coroutine[Any, Any, None]]
Validator = Callable[[PluginContext], Coroutine[Any, Any, list[str]]]


@dataclass
class Plugin:
    """Плагин: метаданные + callbacks для каждого capability."""

    meta: PluginMeta
    prompt_block: PromptBlockGetter | None = None
    post_day_hook: PostDayHook | None = None
    results_format: ResultsFormatter | None = None
    pre_day_hook: PreDayHook | None = None
    validator: Validator | None = None
    echo_trigger: PostDayHook | None = None
    personal_echo: PostDayHook | None = None

    def has_capability(self, cap: Capability) -> bool:
        return cap in self.meta.capabilities


class PluginRegistry:
    """Реестр плагинов: регистрация, поиск, вызов hooks.

    Глобальный реестр (один на процесс) — аналог router в aiogram.
    """

    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}
        self._sorted_by_capability: dict[Capability, list[Plugin]] = {}

    def register(self, plugin: Plugin) -> None:
        """Регистрирует плагин в реестре."""
        if plugin.meta.name in self._plugins:
            logger.warning("Плагин %s уже зарегистрирован, перезапись", plugin.meta.name)
        self._plugins[plugin.meta.name] = plugin
        # Инвалидация кэша
        self._sorted_by_capability.clear()
        logger.info(
            "Плагин зарегистрирован: %s (capabilities: %s)",
            plugin.meta.name,
            ", ".join(c.value for c in plugin.meta.capabilities),
        )

    def unregister(self, name: str) -> bool:
        """Удаляет плагин из реестра."""
        if name in self._plugins:
            del self._plugins[name]
            self._sorted_by_capability.clear()
            return True
        return False

    def get(self, name: str) -> Plugin | None:
        return self._plugins.get(name)

    def list_plugins(self) -> list[PluginMeta]:
        return [p.meta for p in self._plugins.values()]

    def _by_capability(self, cap: Capability) -> list[Plugin]:
        """Ленивый кэш: плагины с данной capability, отсортированные по priority."""
        if cap not in self._sorted_by_capability:
            matching = [
                p
                for p in self._plugins.values()
                if p.meta.enabled and p.has_capability(cap)
            ]
            self._sorted_by_capability[cap] = sorted(
                matching, key=lambda p: p.meta.priority
            )
        return self._sorted_by_capability[cap]

    # ── Вызов hooks ──

    async def collect_prompt_blocks(self, ctx: PluginContext) -> list[str]:
        """Собирает prompt blocks от всех плагинов с PROMPT_BLOCK capability."""
        blocks = []
        for plugin in self._by_capability(Capability.PROMPT_BLOCK):
            if plugin.prompt_block is not None:
                try:
                    block = await plugin.prompt_block(ctx)
                    if block:
                        blocks.append(block)
                except Exception:
                    logger.exception(
                        "Prompt block плагина %s не удался", plugin.meta.name
                    )
        return blocks

    async def collect_results_format(self, ctx: PluginContext) -> list[str]:
        """Собирает дополнительные строки итогов от плагинов с RESULTS_FORMAT."""
        lines = []
        for plugin in self._by_capability(Capability.RESULTS_FORMAT):
            if plugin.results_format is not None:
                try:
                    line = await plugin.results_format(ctx)
                    if line:
                        lines.append(line)
                except Exception:
                    logger.exception(
                        "Results format плагина %s не удался", plugin.meta.name
                    )
        return lines

    async def run_post_day_hooks(self, ctx: PluginContext) -> None:
        """Запускает post-day hooks от всех плагинов."""
        for plugin in self._by_capability(Capability.POST_DAY_HOOK):
            if plugin.post_day_hook is not None:
                try:
                    await plugin.post_day_hook(ctx)
                except Exception:
                    logger.exception(
                        "Post-day hook плагина %s не удался", plugin.meta.name
                    )

    async def run_pre_day_hooks(self, ctx: PluginContext) -> None:
        """Запускает pre-day hooks от всех плагинов."""
        for plugin in self._by_capability(Capability.PRE_DAY_HOOK):
            if plugin.pre_day_hook is not None:
                try:
                    await plugin.pre_day_hook(ctx)
                except Exception:
                    logger.exception(
                        "Pre-day hook плагина %s не удался", plugin.meta.name
                    )

    async def run_validators(self, ctx: PluginContext) -> list[str]:
        """Запускает все валидаторы, собирает ошибки."""
        errors = []
        for plugin in self._by_capability(Capability.VALIDATOR):
            if plugin.validator is not None:
                try:
                    plugin_errors = await plugin.validator(ctx)
                    errors.extend(plugin_errors)
                except Exception:
                    logger.exception(
                        "Validator плагина %s не удался", plugin.meta.name
                    )
        return errors


# ── Глобальный реестр ──
registry = PluginRegistry()


# ── Базовый класс для удобства создания плагинов ──

class BasePlugin:
    """Базовый класс для плагинов. Наследуй и переопределяй hooks."""

    name: str = "unnamed"
    description: str = ""
    version: str = "1.0.0"
    capabilities: frozenset[Capability] = frozenset()
    priority: int = HookOrder.NORMAL

    def as_plugin(self) -> Plugin:
        """Конвертирует BasePlugin в Plugin для регистрации."""
        return Plugin(
            meta=PluginMeta(
                name=self.name,
                description=self.description,
                version=self.version,
                capabilities=self.capabilities,
                priority=self.priority,
            ),
            prompt_block=self._prompt_block if Capability.PROMPT_BLOCK in self.capabilities else None,
            post_day_hook=self._post_day_hook if Capability.POST_DAY_HOOK in self.capabilities else None,
            results_format=self._results_format if Capability.RESULTS_FORMAT in self.capabilities else None,
            pre_day_hook=self._pre_day_hook if Capability.PRE_DAY_HOOK in self.capabilities else None,
            validator=self._validator if Capability.VALIDATOR in self.capabilities else None,
        )

    async def _prompt_block(self, ctx: PluginContext) -> str | None:
        return None

    async def _post_day_hook(self, ctx: PluginContext) -> None:
        pass

    async def _results_format(self, ctx: PluginContext) -> str | None:
        return None

    async def _pre_day_hook(self, ctx: PluginContext) -> None:
        pass

    async def _validator(self, ctx: PluginContext) -> list[str]:
        return []

    def register_self(self) -> None:
        """Регистрирует себя в глобальном реестре."""
        registry.register(self.as_plugin())
