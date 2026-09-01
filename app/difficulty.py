"""Dynamic Difficulty Adjustment — адаптивные правила победы.

Архитектурный инсайт: сегодня win rule выбирается случайно или по
календарю (midpoint). DDA делает_rule_адаптивным к engagement:
- Низкая явка → упрощаем (MAJORITY — большинство правит)
- Высокая ставки → усложняем (MEDIAN — середина правит)
- С_balanced → MINORITY — меньшинство правит

Формула:
  engagement_score = 0.4 * turnout_ratio + 0.3 * stake_intensity + 0.3 * diversity
  difficulty_threshold = map(score to rule)

Формула не влияет на sealed days — там rule всегда скрыт.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DifficultyMetrics:
    """Метрики для определения сложности."""

    turnout_ratio: float  # доля проголосовавших от активных (0-1)
    stake_intensity: float  # средняя ставка на игрока (нормализовано)
    diversity: float  # разнообразие голосов (0-1, 1 = равномерно)
    recent_trend: str  # "growing" / "stable" / "declining"

    @property
    def engagement_score(self) -> float:
        """Комбинированный показатель engagement (0-1)."""
        return (
            0.4 * self.turnout_ratio
            + 0.3 * min(1.0, self.stake_intensity)
            + 0.3 * self.diversity
        )


def _compute_diversity(counts: dict[int, int]) -> float:
    """Разнообразие голосов: энтропия / max_entropy.

    1.0 = голоса распределены равномерно
    0.0 = все голоса за один путь
    """
    total = sum(counts.values())
    if total == 0:
        return 0.0

    import math

    max_entropy = math.log(3)  # 3 пути
    entropy = 0.0
    for count in counts.values():
        if count > 0:
            p = count / total
            entropy -= p * math.log(p)

    return entropy / max_entropy if max_entropy > 0 else 0.0


def _estimate_trend(recent_turnouts: list[int], window: int = 5) -> str:
    """Оценка тренда явки по последним дням.

    'growing' если тренд растёт, 'declining' если падает, иначе 'stable'.
    """
    if len(recent_turnouts) < 2:
        return "stable"

    recent = recent_turnouts[-window:]
    if len(recent) < 2:
        return "stable"

    # Простая линейная регрессия
    n = len(recent)
    x_mean = (n - 1) / 2
    y_mean = sum(recent) / n

    numerator = sum((i - x_mean) * (recent[i] - y_mean) for i in range(n))
    denominator = sum((i - x_mean) ** 2 for i in range(n))

    if denominator == 0:
        return "stable"

    slope = numerator / denominator
    if slope > 0.5:
        return "growing"
    elif slope < -0.5:
        return "declining"
    return "stable"


def compute_difficulty_metrics(
    counts: dict[int, int],
    total_stakes: int,
    player_count: int,
    recent_turnouts: list[int] | None = None,
) -> DifficultyMetrics:
    """Вычисляет метрики сложности для дня."""
    total_votes = sum(counts.values())
    turnout_ratio = total_votes / max(player_count, 1)
    stake_intensity = total_stakes / max(total_votes, 1) / 1e9  # нормализация до Gram
    diversity = _compute_diversity(counts)
    trend = _estimate_trend(recent_turnouts or [])

    return DifficultyMetrics(
        turnout_ratio=min(1.0, turnout_ratio),
        stake_intensity=stake_intensity,
        diversity=diversity,
        recent_trend=trend,
    )


def select_difficulty(
    metrics: DifficultyMetrics,
    day_index: int,
    is_sealed: bool = False,
    is_midpoint: bool = False,
) -> str:
    """Выбирает уровень сложности на основе метрик.

    Возвращает: "easy" / "medium" / "hard" / "adaptive"

    Формула:
    - engagement < 0.3 → easy (MAJORITY — предсказуемо)
    - engagement 0.3-0.6 → medium (MINORITY — неожиданно)
    - engagement > 0.6 → hard (MEDIAN — сложно)
    - Sealed day → always adaptive (не влияет на выбор)
    """
    if is_sealed:
        return "adaptive"

    score = metrics.engagement_score

    # Корректировка по тренду
    if metrics.recent_trend == "declining":
        score -= 0.1  # Упрощаем при падении engagement
    elif metrics.recent_trend == "growing":
        score += 0.05  # Немного усложняем при росте

    # Корректировка по midpoint (середина сезона — сложнее)
    if is_midpoint:
        score += 0.1

    if score < 0.3:
        return "easy"
    elif score < 0.6:
        return "medium"
    else:
        return "hard"


# Маппинг уровня сложности на WinRule
_DIFFICULTY_TO_RULE = {
    "easy": "majority",
    "medium": "minority",
    "hard": "median",
    "adaptive": None,  # определяется ниже
}


def select_win_rule(
    metrics: DifficultyMetrics,
    day_index: int,
    is_sealed: bool = False,
    is_midpoint: bool = False,
    seed: int | None = None,
) -> str:
    """Выбирает WinRule на основе DDA.

    Возвращает значение WinRule ("majority" / "minority" / "median").
    """
    from app.models import WinRule

    difficulty = select_difficulty(metrics, day_index, is_sealed, is_midpoint)

    if difficulty == "adaptive":
        # Для sealed/adaptive — детерминированный выбор по seed
        if seed is not None:
            import random

            rng = random.Random(f"dda:{seed}")
            return rng.choice(list(WinRule)).value
        return secrets.choice(list(WinRule)).value

    rule_str = _DIFFICULTY_TO_RULE.get(difficulty)
    if rule_str:
        return rule_str

    # Fallback
    return secrets.choice(list(WinRule)).value


def format_difficulty_hint(metrics: DifficultyMetrics, difficulty: str) -> str:
    """Подсказка о сложности для хранителя (в пульте)."""
    score = metrics.engagement_score
    trend = {
        "growing": "📈 растёт",
        "stable": "➡️ стабильно",
        "declining": "📉 падает",
    }.get(metrics.recent_trend, "—")

    return (
        f"🎯 Сложность: {difficulty}\n"
        f"   Engagement: {score:.2f} "
        f"(явка {metrics.turnout_ratio:.0%}, "
        f"разнообразие {metrics.diversity:.2f})\n"
        f"   Тренд: {trend}"
    )
