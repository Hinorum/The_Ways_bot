"""Центральный реестр ключей watcher_state и одноразовых маркеров.

Все ключи-константы и генераторы префиксных ключей собраны в одном месте.
Модули-«владельцы» (ops/ton_watch/season/...) импортируют канонические имена
отсюда и пробрасывают их дальше — старые точки импорта не ломаются.

Правило: новый ключ добавляется здесь, а не в теле модуля. Удаление ключа —
осознанное решение, проходящее через git blame этого файла.
"""

from __future__ import annotations

# --- Операционная наблюдаемость / алерты (app/ops.py) ---

TICK_KEY = "last_tick_iso"
ALERT_WATCHER_KEY = "alert_watcher_ts"
ALERT_QUEUE_KEY = "alert_queue_ts"
ALERT_DEAD_KEY = "alert_dead_ts"
ALERT_TICK_KEY = "alert_tick_ts"
ALERT_BALANCE_KEY = "alert_balance_ts"
ALERT_REFUND_KEY = "alert_refund_ts"
ALERT_STAKE_KEY = "alert_stake_ts"

# Пауза игры (стоп-кран) и режим «со ставками» / «без ставок».
PAUSE_KEY = "game_paused_iso"
PAUSE_REASON_KEY = "game_paused_reason"
MONEY_MODE_KEY = "money_mode_on"

# --- Отношения с NPC (app/relations.py) ---

RELATION_KEY = "npc_relations"
# Парные связи между лицами мира (NPC↔NPC): какая пара дружна, какая в раздоре.
PAIR_RELATION_KEY = "npc_pair_relations"

# --- Лидерборды (app/leaderboard.py) ---

MARKER_KEY = "leaderboard_settled_through"
WEEKLY_MARKER_KEY = "weekly_settled_through"
MONTH_READY_KEY = "month_leaderboard_ready"
WEEK_READY_KEY = "week_leaderboard_ready"

# --- Сезон / сюжет (app/season.py) ---

VILLAIN_KEY = "villain_plot"
RUN_START_KEY = "run_season_anchor"

# --- TON-watcher (app/ton_watch.py) ---

CURSOR_KEY = "ton_watch_cursor_utime"
BEAT_KEY = "ton_watch_beat_iso"
SOURCE_KEY = "ton_watch_last_source"
WALLET_NORM_KEY = "wallet_norm_v1"

# --- Арт-директор (app/art_director.py) ---

ANCHOR_KEY = "art_anchor"

# --- Генетическая эволюция промптов (app/gepa.py / app/narrative_ai.py) ---

GEPA_POPULATION_KEY = "gepa_population"


# ---------- Префиксные ключи (строятся по идентификатору) ----------

# Хранилища-поток: ключ содержит день/раунд, поэтому читаются и пишутся
# только через генераторы ниже — строка-формат живёт в одном месте.


def art_bible_key(day_index: int) -> str:
    """Полная визуальная библия дня (app/rounds.py)."""
    return f"art_bible:{day_index}"


def img_stubs_key(day_index: int) -> str:
    """Отложенный апгрейд PIL-заглушек картинок (app/rounds.py)."""
    return f"img_stubs:{day_index}"


def day_projection_key(day_index: int) -> str:
    """Исторический кэш DayProjection; вводится для совместимости формата."""
    return f"day_projection:{day_index}"


def micro_event_key(round_id: int) -> str:
    """Маркер «микрособытие дня уже разыграно» (app/scheduler.py)."""
    return f"micro_event:{round_id}"


def teaser_key(round_id: int) -> str:
    """Маркер «тизер следующего дня уже отправлен» (app/scheduler.py)."""
    return f"teaser:{round_id}"


def pecho_key(round_id: int) -> str:
    """Маркер «личное эхо уже ушло игроку» (app/scheduler.py)."""
    return f"pecho:{round_id}"


def sniff_key(player_id: int, round_id: int) -> str:
    """Маркер дневного лимита команды «нюх» (app/handlers/player.py)."""
    return f"sniff:{player_id}:{round_id}"


def memquiz_key(player_id: int, round_id: int) -> str:
    """Маркер «квиз памяти уже закрыт» (app/handlers/player.py)."""
    return f"memquiz:{player_id}:{round_id}"