"""NPC Chain-of-Thought — внутренний монолог NPC перед действием.

Архитектурный инсайт: сегодня NPC описываются одним sentiment score
и фиксированными репликами. Chain-of-thought даёт каждому NPC:
1. Внутренний монолог (что он думает о стае)
2. Мотивацию (что он хочет сделать)
3. Действие (конкретный поступок)

Это делает NPC живыми: один и тот же sentiment score может привести
к разным действиям в зависимости от контекста дня.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.relations import _TONES

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NPCCogResult:
    """Результат chain-of-thought для одного NPC."""

    name: str
    sentiment: int
    tone: str
    inner_thought: str  # Внутренний монолог
    motivation: str  # Что хочет сделать
    action_hint: str  # Подсказка для DM: что NPC делает в этой сцене
    focus_line: str  # Готовая реплика/действие для инжекта в промпт

    def to_prompt_block(self) -> str:
        """Форматирует результат для блока промпта."""
        return (
            f"[{self.name.upper()} — {self.tone} (отношение {self.sentiment:+d})]\n"
            f"Мысли: {self.inner_thought}\n"
            f"Мотив: {self.motivation}\n"
            f"Действие: {self.action_hint}"
        )


# ── Шаблоны внутренних монологов по тону и NPC ──

_INNER_THOUGHTS = {
    "liner": {
        "devoted": [
            "Стая — мой путь. Я веду их, даже если они не понимают куда.",
            "Каждый выбор стаи — это доверие мне. Я не подведу.",
            "Тропа передо мной ясна, за мной — только верные.",
        ],
        "cautious": [
            "Стая идёт, но я не уверен, что они видят ловушки.",
            "Мне нужно быть осторожнее. Один неверный шаг — и мы все упадём.",
            "Доверие ещё не потеряно, но оно хрупкое.",
        ],
        "wary": [
            "Стая отдаляется. Я чувствую это в каждом голосовании.",
            "Они не слушают. Но я продолжу вести — это моя роль.",
            "Раскол растёт. Мне нужно напомнить им, кто мы.",
        ],
        "hostile": [
            "Стая стала враждебна. Но я не отступлю.",
            "Они выбрали путь предательства. Я запомню это.",
            "Лабиринт пожирает тех, кто теряет друг друга.",
        ],
    },
    "archivist": {
        "devoted": [
            "Каждый голос — запись в архиве. Я храню их историю.",
            "Стая создаёт канон, а я — его свидетель.",
            "Архив растёт с каждым днём. Это beautiful.",
        ],
        "cautious": [
            "Данные говорят одно, но сердце стаи — другое.",
            "Мне нужно аккуратнее интерпретировать записи.",
            "Архив должен оставаться нейтральным.",
        ],
        "wary": [
            "Записи искажаются. Кто-то пытается изменить историю?",
            "Стая забывает свои собственные решения.",
            "Архив хранит правду, даже если она неудобна.",
        ],
        "hostile": [
            "Записи под угрозой. Мне нужно защитить архив.",
            "Стая уничтожает собственную историю.",
            "Я — последний страж памяти. Не сдамся.",
        ],
    },
    "master": {
        "devoted": [
            "Стая сильна, когда работает вместе. Я помогу им.",
            "Мой опыт — их щит. Они не знают, сколько опасностей я отвёл.",
            "Каждый день — возможность стать крепче.",
        ],
        "cautious": [
            "Стаю нужно укрепить. Слишком много рисков.",
            "Я вижу слабости. Но говорить прямо — значит напугать.",
            "Дипломатия важнее силы. Пока.",
        ],
        "wary": [
            "Стая ослабевает. Пора действовать решительнее.",
            "Они не понимают, что лабиринт — не игра.",
            "Мне придётся взять инициативу.",
        ],
        "hostile": [
            "Стая отвергла мой опыт. Пусть пожинают последствия.",
            "Лабиринт учит тех, кто отказывается учиться сам.",
            "Я защищал их, а они выбрали хаос.",
        ],
    },
    "heretic": {
        "devoted": [
            "Стая наконец-то видит истину. Я помогу им увидеть больше.",
            "Ересь — это смелость думать иначе. Стая растёт.",
            "Каждый выбор — вызов порядку. Это хорошо.",
        ],
        "cautious": [
            "Стая на грани. Одно неверное слово — и меня изгонят.",
            "Истина должна подаваться дозированно.",
            "Провокация — искусство. Нужно знать меру.",
        ],
        "wary": [
            "Стая скатывается в conformity. Пора напомнить им о свободе.",
            "Законы лабиринта — иллюзия. Но стая в них верит.",
            "Мне нужно снова потрясти основы.",
        ],
        "hostile": [
            "Стая стала инструментом порядка. Отвратительно.",
            "Я разоблачу их лицемерие. Рано или поздно.",
            "Лабиринт создан для тех, кто смеет бросить вызов.",
        ],
    },
}

# ── Мотивации по тону ──

_MOTIVATIONS = {
    "devoted": "Укрепить связь со стаей, поддержать их выбор",
    "cautious": "Предупредить о рисках, не напугая",
    "wary": "Восстановить доверие или принять khó khăn решение",
    "hostile": "Дистанцироваться или противодействовать",
}

# ── Действия по NPC и тону ──

_ACTIONS = {
    "liner": {
        "devoted": "Показывает тропу, делится наблюдениями о лабиринте",
        "cautious": "Останавливает стаю перед потенциальной ловушкой",
        "wary": "Молча ведёт, избегая разговоров",
        "hostile": "Уходит вперёд, не оглядываясь",
    },
    "archivist": {
        "devoted": "Открывает страницу архива, показывая важную запись",
        "cautious": "Цитирует осторожно, опуская тревожные детали",
        "wary": "Скрывает часть записей, показывая только «безопасные»",
        "hostile": "Запирает архив, отказываясь делиться",
    },
    "master": {
        "devoted": "Делится стратегией, помогает стае планировать",
        "cautious": "Предлагает альтернативный маршрут",
        "wary": "Принимает командование, не спрашивая разрешения",
        "hostile": "Действует в одиночку, игнорируя стаю",
    },
    "heretic": {
        "devoted": "Провоцирует стаю на смелый выбор",
        "cautious": "Задаёт неудобные вопросы, но деликатно",
        "wary": "Публично ставит под сомнение решение стаи",
        "hostile": "Открыто бросает вызов, провоцируя конфликт",
    },
}


def _pick_thought(name: str, tone: str, seed: int) -> str:
    """Детерминированный выбор внутреннего монолога."""
    import random

    thoughts = _INNER_THOUGHTS.get(name, {}).get(tone, _INNER_THOUGHTS.get(name, {}).get("cautious", ["..."]))
    return thoughts[seed % len(thoughts)]


def _pick_action(name: str, tone: str, seed: int) -> str:
    """Детерминированный выбор действия."""
    import random

    actions = _ACTIONS.get(name, {}).get(tone, "Наблюдает за стаей")
    return actions


def generate_npc_cog(
    name: str,
    sentiment: int,
    day_index: int,
    winning_tag: str | None = None,
    voter_count: int = 0,
) -> NPCCogResult:
    """Генерирует chain-of-thought для NPC на основе sentiment и контекста.

    Детерминированно: одинаковые входы → одинаковые выходы.
    """
    tone_data = _TONES.get(sentiment, ("neutral", "безразличен"))
    tone = tone_data[0] if isinstance(tone_data, tuple) else str(tone_data)

    inner_thought = _pick_thought(name, tone, day_index)
    motivation = _MOTIVATIONS.get(tone, "Наблюдать за стаей")
    action_hint = _pick_action(name, tone, day_index)

    # Формируем focus line — готовую реплику для DM
    focus_line = (
        f"{name.capitalize()} {tone}: «{inner_thought}» "
        f"—行动: {action_hint}"
    )

    return NPCCogResult(
        name=name,
        sentiment=sentiment,
        tone=tone,
        inner_thought=inner_thought,
        motivation=motivation,
        action_hint=action_hint,
        focus_line=focus_line,
    )


def npc_cogs_block(
    cogs: list[NPCCogResult],
    max_lines: int = 8,
) -> str:
    """Форматирует блок chain-of-thought для промпта.

    Ограничивает количество строк, чтобы не раздувать контекст.
    """
    if not cogs:
        return ""

    lines = ["Chain-of-thought NPC:"]
    for cog in cogs[:max_lines]:
        lines.append(cog.to_prompt_block())
        lines.append("")

    return "\n".join(lines)


async def generate_all_npc_cogs(
    relations: dict[str, int],
    day_index: int,
    winning_tag: str | None = None,
    voter_count: int = 0,
) -> list[NPCCogResult]:
    """Генерирует chain-of-thought для всех NPC.

    relations: {npc_name: sentiment_value}
    """
    cogs = []
    for name, sentiment in relations.items():
        cog = generate_npc_cog(
            name=name,
            sentiment=sentiment,
            day_index=day_index,
            winning_tag=winning_tag,
            voter_count=voter_count,
        )
        cogs.append(cog)
    return cogs
