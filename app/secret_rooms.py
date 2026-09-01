"""Тайные комнаты — варианты探索 через карты.

При выборе карты есть шанс обнаружить тайную комнату с уникальным
эффектом. Это добавляет элемент исследования без网格的地图.
"""

from __future__ import annotations

from dataclasses import dataclass
import random

from app.models import WorldScar


@dataclass
class SecretRoom:
    """Тайная комната."""
    key: str
    name: str
    description: str
    effect_type: str  # "buff", "debuff", "item", "scar", "npc"
    effect_value: dict | None = None
    discovery_chance: float = 0.15  # 15% шанс обнаружения
    requires_scar: str | None = None  # Требуется шрам для доступа
    one_time: bool = True  # Можно обнаружить только один раз


# Определения тайных комнат
SECRET_ROOMS: dict[str, SecretRoom] = {
    "forgotten_cache": SecretRoom(
        key="forgotten_cache",
        name="Забытый тайник",
        description="Стая находит спрятанные припасы в стене лабиринта",
        effect_type="buff",
        effect_value={"hunger": -3, "thirst": -2},
        discovery_chance=0.2,
    ),
    "wounded_dog": SecretRoom(
        key="wounded_dog",
        name="Раненый пес",
        description="В темноте скулит раненый незнакомец. Помочь — или пройти мимо?",
        effect_type="npc",
        effect_value={"health": +1, "hope": +1},
        discovery_chance=0.15,
    ),
    "echo_chamber": SecretRoom(
        key="echo_chamber",
        name="Комната эха",
        description="Стены повторяют голоса прошлых дней. Мир напоминает о себе",
        effect_type="scar",
        effect_value={"scar_key": "whisper_of_trick"},
        discovery_chance=0.1,
    ),
    "dark_tunnel": SecretRoom(
        key="dark_tunnel",
        name="Тёмный тоннель",
        description="Коридор уходит вниз. Там что-то движется",
        effect_type="debuff",
        effect_value={"health": -2, "paranoia": +2},
        discovery_chance=0.15,
    ),
    "ancient_inscription": SecretRoom(
        key="ancient_inscription",
        name="Древняя надпись",
        description="На стене выцарапаны символы. Кто-то был здесь до стаи",
        effect_type="buff",
        effect_value={"hope": +2},
        discovery_chance=0.12,
    ),
    "collapsed_passage": SecretRoom(
        key="collapsed_passage",
        name="Обрушившийся проход",
        description="Потолок обвалился. За завалом — что-то блестит",
        effect_type="item",
        effect_value={"hunger": -2, "health": -1},
        discovery_chance=0.1,
        requires_scar="burned_path",
    ),
    "spirit_well": SecretRoom(
        key="spirit_well",
        name="Колодец духа",
        description="Из трещины в полу поднимается тёплый свет",
        effect_type="buff",
        effect_value={"health": +2, "fatigue": -2},
        discovery_chance=0.08,
        requires_scar="warm_hearth",
    ),
    "memory_pool": SecretRoom(
        key="memory_pool",
        name="Бассейн воспоминаний",
        description="Вода отражает не лицо, а прошлое. Стая видит другие пути",
        effect_type="buff",
        effect_value={"cunning": +1},
        discovery_chance=0.1,
    ),
}


def check_secret_room_discovery(
    rng: random.Random,
    active_scars: list[WorldScar],
    discovered_rooms: set[str],
) -> SecretRoom | None:
    """Проверяет, обнаружена ли тайная комната.

    Args:
        rng: Генератор случайных чисел
        active_scars: Активные шрамы мира
        discovered_rooms: Уже обнаруженные комнаты

    Returns:
        Обнаруженная комната или None
    """
    available = []
    scar_keys = {s.scar_key for s in active_scars}

    for room in SECRET_ROOMS.values():
        if room.key in discovered_rooms:
            continue
        if room.requires_scar and room.requires_scar not in scar_keys:
            continue
        available.append(room)

    if not available:
        return None

    # Сортируем по шансу (от низкого к высокому)
    available.sort(key=lambda r: r.discovery_chance)

    # Бросаем кубик для каждой комнаты
    for room in available:
        if rng.random() < room.discovery_chance:
            return room

    return None


def format_room_discovery(room: SecretRoom) -> str:
    """Форматирует текст обнаружения комнаты для промпта."""
    return (
        f"ТАЙНАЯ КОМНАТА: {room.name}\n"
        f"{room.description}\n"
        f"Эффект: {room.effect_type}"
    )


def apply_room_effect(effect_value: dict | None, pack_needs) -> dict:
    """Применяет эффект комнаты к потребностям стаи.

    Возвращает словарь с описанием произошедшего.
    """
    if effect_value is None:
        return {"text": "Ничего не изменилось"}

    result_parts = []

    for key, value in effect_value.items():
        if key == "hunger":
            pack_needs.hunger = max(0, min(10, pack_needs.hunger + value))
            result_parts.append(f"голод {'+' if value > 0 else ''}{value}")
        elif key == "thirst":
            pack_needs.thirst = max(0, min(10, pack_needs.thirst + value))
            result_parts.append(f"жажда {'+' if value > 0 else ''}{value}")
        elif key == "health":
            pack_needs.health = max(0, min(10, pack_needs.health + value))
            result_parts.append(f"здоровье {'+' if value > 0 else ''}{value}")
        elif key == "fatigue":
            result_parts.append(f"усталость {'+' if value > 0 else ''}{value}")
        elif key == "hope":
            result_parts.append(f"надежда {'+' if value > 0 else ''}{value}")
        elif key == "paranoia":
            result_parts.append(f"паранойя {'+' if value > 0 else ''}{value}")
        elif key == "scar_key":
            result_parts.append(f"шрам: {value}")

    return {
        "text": "Изменения: " + ", ".join(result_parts) if result_parts else "Ничего не изменилось",
        "effect_value": effect_value,
    }


def get_room_text_for_prompt(discovered_rooms: set[str]) -> str:
    """Форматирует текст обнаруженных комнат для промпта."""
    if not discovered_rooms:
        return ""

    lines = ["ОБНАРУЖЕННЫЕ ТАЙНЫЕ КОМНАТЫ:"]
    for room_key in discovered_rooms:
        if room_key in SECRET_ROOMS:
            room = SECRET_ROOMS[room_key]
            lines.append(f"- {room.name}: {room.description}")

    return "\n".join(lines) if len(lines) > 1 else ""
