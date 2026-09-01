"""Эмоциональный профиль стаи — fatigue/hope/paranoia.

Три параметра (0-10) накапливаются от выборов и влияют на:
- Тон атмосферных пэдов
- Распределение карточек
- Доступность NPC-взаимодействий
- Варианты финала
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EmotionalState


# Шаги изменения эмоций по тегу победившей карты
EMOTION_SHIFTS: dict[str, dict[str, int]] = {
    "risk": {"fatigue": +1, "hope": -1, "paranoia": 0},
    "care": {"fatigue": -1, "hope": +1, "paranoia": 0},
    "cunning": {"fatigue": 0, "hope": -1, "paranoia": +1},
}

_MIN, _MAX = 0, 10


@dataclass
class EmotionProfile:
    """Текущий эмоциональный профиль стаи."""
    fatigue: int = 0
    hope: int = 0
    paranoia: int = 0

    def clamp(self) -> None:
        """Ограничивает значения диапазоном [0, 10]."""
        self.fatigue = max(_MIN, min(_MAX, self.fatigue))
        self.hope = max(_MIN, min(_MAX, self.hope))
        self.paranoia = max(_MIN, min(_MAX, self.paranoia))


def apply_emotion_shift(profile: EmotionProfile, winner_tag: str | None) -> EmotionProfile:
    """Применяет сдвиг эмоций по тегу победившей карты. Мутирует и возвращает."""
    if winner_tag not in EMOTION_SHIFTS:
        return profile
    shifts = EMOTION_SHIFTS[winner_tag]
    profile.fatigue += shifts.get("fatigue", 0)
    profile.hope += shifts.get("hope", 0)
    profile.paranoia += shifts.get("paranoia", 0)
    profile.clamp()
    return profile


def get_emotion_phase(profile: EmotionProfile) -> str:
    """Определяет эмоциональную фазу стаи."""
    if profile.fatigue >= 7:
        return "exhausted"
    if profile.hope >= 7:
        return "inspired"
    if profile.paranoia >= 7:
        return "suspicious"
    return "balanced"


def get_emotion_tint(profile: EmotionProfile) -> str:
    """Возвращает тон текста на основе эмоционального профиля."""
    phase = get_emotion_phase(profile)
    tints = {
        "exhausted": "стая устала, мир кажется тяжёлым и медленным",
        "inspired": "стая полна решимости, мир открыт и полон возможностей",
        "suspicious": "стая настороже, каждый шорох кажется ловушкой",
        "balanced": "стая в равновесии, мир течёт нормально",
    }
    return tints[phase]


def get_card_distribution_modifier(profile: EmotionProfile) -> dict[str, float]:
    """Возвращает модификатор распределения карточек на основе эмоций.

    Возвращает веса для risk/care/cunning. Если fatigue высокий — больше care,
    если hope высокий — больше risk, если paranoia высокий — больше cunning.
    При равных эмоциях — стандартное распределение.
    """
    base = {"risk": 0.33, "care": 0.33, "cunning": 0.34}

    # Определяем доминирующую эмоцию (разница от среднего)
    avg = (profile.fatigue + profile.hope + profile.paranoia) / 3
    fatigue_dom = profile.fatigue - avg
    hope_dom = profile.hope - avg
    paranoia_dom = profile.paranoia - avg

    # Применяем модификатор только если доминирует одна эмоция
    max_dom = max(fatigue_dom, hope_dom, paranoia_dom)
    if max_dom < 1:  # Нет явного доминирования
        return base

    if fatigue_dom == max_dom:
        base["care"] += 0.15
        base["risk"] -= 0.10
        base["cunning"] -= 0.05
    elif hope_dom == max_dom:
        base["risk"] += 0.15
        base["care"] -= 0.10
        base["cunning"] -= 0.05
    elif paranoia_dom == max_dom:
        base["cunning"] += 0.15
        base["risk"] -= 0.10
        base["care"] -= 0.05

    # Нормализация
    total = sum(base.values())
    return {k: v / total for k, v in base.items()}


def emotion_block_for_prompt(profile: EmotionProfile) -> str | None:
    """Возвращает блок для промпта главы. None — все нейтральны."""
    phase = get_emotion_phase(profile)
    if phase == "balanced":
        return None

    descriptions = {
        "exhausted": (
            f"УСТАЛОСТЬ СТАИ ({profile.fatigue}/10): стая на пределе. "
            "Осторожные действия, тихие голоса,沉重ные лапы. "
            "Мир кажется медленным и тяжёлым."
        ),
        "inspired": (
            f"НАДЕЖДА СТАИ ({profile.hope}/10): стая полна решимости. "
            "Смелые действия, яркие голоса, быстрые лапы. "
            "Мир открыт и полон возможностей."
        ),
        "suspicious": (
            f"ПАРАНОЙЯ СТАИ ({profile.paranoia}/10): стая настороже. "
            "Хитрые действия, шёпот, косые взгляды. "
            "Каждый шорох кажется ловушкой."
        ),
    }
    return descriptions.get(phase, "")


async def load_emotional_state(session: AsyncSession) -> EmotionProfile:
    """Загружает эмоциональное состояние из БД."""
    result = await session.execute(select(EmotionalState).limit(1))
    row = result.scalar_one_or_none()
    if row is None:
        return EmotionProfile()
    return EmotionProfile(
        fatigue=row.fatigue,
        hope=row.hope,
        paranoia=row.paranoia,
    )


async def save_emotional_state(
    session: AsyncSession,
    profile: EmotionProfile,
    current_day: int,
) -> None:
    """Сохраняет эмоциональное состояние в БД."""
    result = await session.execute(select(EmotionalState).limit(1))
    row = result.scalar_one_or_none()
    if row is None:
        row = EmotionalState(
            fatigue=profile.fatigue,
            hope=profile.hope,
            paranoia=profile.paranoia,
            last_updated_day=current_day,
        )
        session.add(row)
    else:
        row.fatigue = profile.fatigue
        row.hope = profile.hope
        row.paranoia = profile.paranoia
        row.last_updated_day = current_day
    await session.flush()


async def process_round_emotions(
    session: AsyncSession,
    winner_tag: str | None,
    current_day: int,
) -> EmotionProfile:
    """Обрабатывает эмоции после завершения раунда. Возвращает обновлённый профиль."""
    profile = await load_emotional_state(session)
    apply_emotion_shift(profile, winner_tag)
    await save_emotional_state(session, profile, current_day)
    return profile
