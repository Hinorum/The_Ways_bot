"""Деревья последствий — ветвящиеся цепочки из выборов стаи.

Каждое значимое действие может создать ветвь, которая развивается
через несколько дней с вариантами выбора. Ветви влияют на:
- Текст главы (активные ветви в промпте)
- Отношения с NPC
- Шрамы мира
- Эмоциональный профиль
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ConsequenceBranch


@dataclass
class BranchStage:
    """Стадия ветви последствий."""
    text: str  # Текст для промпта
    choices: dict[str, dict] | None = None  # tag → effects
    delay_days: int = 0  # Задержка до следующей стадии
    auto_advance: bool = False  # Автоматический переход


@dataclass
class ConsequenceTree:
    """Дерево последствий — набор стадий с вариантами выбора."""
    key: str
    trigger_card: str  # Название карты-триггера
    stages: list[BranchStage] = field(default_factory=list)


# Определения деревьев
CONSEQUENCE_TREES: dict[str, ConsequenceTree] = {
    "foreign_pack_debt": ConsequenceTree(
        key="foreign_pack_debt",
        trigger_card="Клыки на кости",
        stages=[
            BranchStage(
                text="Чужая стая запомнила лицо — долг висит в воздухе.",
                delay_days=5,
                auto_advance=True,
            ),
            BranchStage(
                text="Чужая стая пришла за долгом. Выбор: отдать или драться.",
                choices={
                    "care": {"effect": "relations +1, branch resolved"},
                    "risk": {"effect": "fight, branch → stage_2"},
                },
            ),
            BranchStage(
                text="Чужая стая вернулась с союзниками. Выбор: переговоры или бой.",
                choices={
                    "cunning": {"effect": "negotiate, branch resolved"},
                    "risk": {"effect": "battle, member lost, branch resolved"},
                },
            ),
        ],
    ),
    "burned_bridge": ConsequenceTree(
        key="burned_bridge",
        trigger_card="Сжечь мост",
        stages=[
            BranchStage(
                text="Мост сгорел. Возвращения нет. Стая идёт только вперёд.",
                delay_days=3,
                auto_advance=True,
            ),
            BranchStage(
                text="За спиной — только пепел. Мир помнит дым.",
                delay_days=7,
                auto_advance=True,
            ),
            BranchStage(
                text="Пепел осел. Лабиринт принял новую форму.",
                delay_days=0,
                auto_advance=True,
            ),
        ],
    ),
    "stolen_food": ConsequenceTree(
        key="stolen_food",
        trigger_card="Клыки на кости",
        stages=[
            BranchStage(
                text="Чужая стая голодает. Еда украдена.",
                delay_days=4,
                auto_advance=True,
            ),
            BranchStage(
                text="Голодные ищут вора. Их следы всё ближе.",
                choices={
                    "cunning": {"effect": "hide, branch resolved"},
                    "risk": {"effect": "confront, branch → stage_2"},
                },
            ),
            BranchStage(
                text="Голодные нашли. Выбор: бой или откуп.",
                choices={
                    "care": {"effect": "share food, peace"},
                    "risk": {"effect": "fight, scars"},
                },
            ),
        ],
    ),
    "labyrinth_doubt": ConsequenceTree(
        key="labyrinth_doubt",
        trigger_card="Третий вариант",
        stages=[
            BranchStage(
                text="Третий путь открыт. Но он ведёт туда, где не было ни одной собаки.",
                delay_days=6,
                auto_advance=True,
            ),
            BranchStage(
                text="Коридоры начали дублироваться. Лабиринт сомневается.",
                choices={
                    "cunning": {"effect": "navigate, branch resolved"},
                    "risk": {"effect": "push through, branch → stage_2"},
                },
            ),
            BranchStage(
                text="Лабиринт принял решение. Двери выровнялись.",
                delay_days=0,
                auto_advance=True,
            ),
        ],
    ),
    "warm_hearth": ConsequenceTree(
        key="warm_hearth",
        trigger_card="Стена для двоих",
        stages=[
            BranchStage(
                text="Стена построена. Та пара спит спокойно.",
                delay_days=5,
                auto_advance=True,
            ),
            BranchStage(
                text="Тёплый очаг привлекает других. Стая растёт.",
                choices={
                    "care": {"effect": "welcome, sanctuary"},
                    "cunning": {"effect": "set rules, control"},
                },
            ),
        ],
    ),
}


def check_tree_trigger(card_title: str) -> ConsequenceTree | None:
    """Проверяет, запускает ли карта дерево последствий."""
    for tree in CONSEQUENCE_TREES.values():
        if tree.trigger_card == card_title:
            return tree
    return None


async def try_dynamic_branch(
    session: AsyncSession,
    card_title: str,
    card_tag: str,
    card_description: str,
    day_index: int,
    recent_events: str = "",
) -> ConsequenceBranch | None:
    """Пытается создать AI-ветвь. При ошибке — фолбэк к хардкоду."""
    data = await generate_dynamic_branch(
        card_title, card_tag, card_description, day_index, recent_events,
    )
    if data is None:
        return None
    branch_key = f"dynamic_{day_index}_{card_tag}"
    stages = data.get("stages", [])
    first_text = stages[0]["text"] if stages else f"Последствия выбора «{card_title}»"
    first_choices = stages[0].get("choices") if stages else None
    return await create_branch_ai(
        session, branch_key, day_index,
        title=data.get("title", card_title),
        stage_text=first_text,
        choices=first_choices or {},
    )


def get_stage_text(tree: ConsequenceTree, stage_idx: int) -> str:
    """Возвращает текст стадии дерева."""
    if 0 <= stage_idx < len(tree.stages):
        return tree.stages[stage_idx].text
    return ""


def get_stage_choices(tree: ConsequenceTree, stage_idx: int) -> dict[str, dict] | None:
    """Возвращает варианты выбора для стадии."""
    if 0 <= stage_idx < len(tree.stages):
        return tree.stages[stage_idx].choices
    return None


def should_advance(tree: ConsequenceTree, stage_idx: int) -> bool:
    """Проверяет, должна ли ветвь автоматически перейти на следующую стадию."""
    if 0 <= stage_idx < len(tree.stages):
        return tree.stages[stage_idx].auto_advance
    return False


def get_delay(tree: ConsequenceTree, stage_idx: int) -> int:
    """Возвращает задержку в днях до следующей стадии."""
    if 0 <= stage_idx < len(tree.stages):
        return tree.stages[stage_idx].delay_days
    return 0


async def load_active_branches(session: AsyncSession, current_day: int) -> list[ConsequenceBranch]:
    """Загружает активные (незавершённые) ветви."""
    result = await session.execute(
        select(ConsequenceBranch).where(ConsequenceBranch.resolved == False)
    )
    return list(result.scalars().all())


async def create_branch(
    session: AsyncSession,
    tree: ConsequenceTree,
    current_day: int,
    ai_title: str = "",
    ai_stage_text: str = "",
    ai_choices: dict[str, str] | None = None,
    ai_resolution: str = "",
) -> ConsequenceBranch:
    """Создаёт новую ветвь последствий с AI-контентом."""
    branch = ConsequenceBranch(
        branch_key=tree.key,
        current_stage=0,
        history_json="[]",
        created_day=current_day,
        resolved=False,
        title=ai_title or tree.trigger_card,
        stage_text=ai_stage_text,
        choices_json=json.dumps(ai_choices or {}, ensure_ascii=False),
        resolution=ai_resolution,
    )
    session.add(branch)
    await session.flush()
    return branch


async def create_branch_ai(
    session: AsyncSession,
    branch_key: str,
    current_day: int,
    title: str,
    stage_text: str,
    choices: dict[str, str],
    resolution: str = "",
) -> ConsequenceBranch:
    """Создаёт новую AI-ветвь без привязки к CONSEQUENCE_TREES."""
    branch = ConsequenceBranch(
        branch_key=branch_key,
        current_stage=0,
        history_json="[]",
        created_day=current_day,
        resolved=False,
        title=title,
        stage_text=stage_text,
        choices_json=json.dumps(choices, ensure_ascii=False),
        resolution=resolution,
    )
    session.add(branch)
    await session.flush()
    return branch


async def generate_dynamic_branch(
    card_title: str,
    card_tag: str,
    card_description: str,
    day_index: int,
    recent_events: str = "",
) -> dict | None:
    """AI генерирует динамическую цепочку последствий на основе выбора стаи.

    Возвращает dict с title, stages (список стадий) или None при ошибке.
    Каждая стадия: {text, delay_days, auto_advance, choices: {tag: effect} | None}.
    """
    from app.story import _chat_completion

    prompt = (
        f"День {day_index}. Карта: «{card_title}» (тег: {card_tag}).\n"
        f"Описание: {card_description}\n"
        f"Недавние события: {recent_events or 'нет'}\n\n"
        "Сгенерируй цепочку последствий этого выбора — 2-3 стадии, "
        "которые развиваются через несколько дней. Каждая стадия — "
        "короткая фраза (1-2 предложения) от третьего лица.\n"
        "Первая стадия — немедленный эффект, auto_advance=true, delay_days=3-5.\n"
        "Вторая стадия — выбор: care (мягко) или risk (силой).\n"
        "Третья стадия (опционально) — финал, auto_advance=true.\n\n"
        'Ответь ТОЛЬКО JSON: {"title": "...", "stages": [{"text": "...", '
        '"delay_days": 4, "auto_advance": true, "choices": null}, '
        '{"text": "...", "delay_days": 0, "auto_advance": false, '
        '"choices": {"care": "effect", "risk": "effect"}}]}'
    )

    result = await _chat_completion(
        [{"role": "user", "content": prompt}],
        timeout=30,
    )
    if result is None:
        return None
    payload, _used_model = result
    try:
        raw = str(payload["choices"][0]["message"]["content"]).strip()
        import re

        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group())
        if "title" not in data or "stages" not in data:
            return None
        return data
    except Exception:
        return None


async def advance_branch(
    session: AsyncSession,
    branch: ConsequenceBranch,
    choice_tag: str | None = None,
    current_day: int = 0,
) -> None:
    """Переводит ветвь на следующую стадию."""
    tree = CONSEQUENCE_TREES.get(branch.branch_key)
    if tree is None:
        return

    # Записываем историю
    history = json.loads(branch.history_json)
    history.append({
        "stage": branch.current_stage,
        "choice": choice_tag,
        "day": current_day,
    })
    branch.history_json = json.dumps(history)

    # Переходим на следующую стадию
    next_stage = branch.current_stage + 1
    if next_stage >= len(tree.stages):
        branch.resolved = True
    else:
        branch.current_stage = next_stage

    await session.flush()


def format_active_branches(branches: list[ConsequenceBranch]) -> str:
    """Форматирует активные ветви для промпта."""
    if not branches:
        return ""

    lines = ["АКТИВНЫЕ ПОСЛЕДСТВИЯ:"]
    for branch in branches:
        # Используем AI-контент из БД если есть
        if branch.stage_text:
            text = branch.stage_text
            choices = json.loads(branch.choices_json) if branch.choices_json else {}
            choice_text = ""
            if choices:
                choice_text = f" Выбор: {'/'.join(choices.keys())}."
            lines.append(f"- {text}{choice_text}")
        else:
            # Фолбэк к хардкоду
            tree = CONSEQUENCE_TREES.get(branch.branch_key)
            if tree is None:
                continue
            text = get_stage_text(tree, branch.current_stage)
            if text:
                choices = get_stage_choices(tree, branch.current_stage)
                choice_text = ""
                if choices:
                    choice_text = f" Выбор: {'/'.join(choices.keys())}."
                lines.append(f"- {text}{choice_text}")

    return "\n".join(lines) if len(lines) > 1 else ""
