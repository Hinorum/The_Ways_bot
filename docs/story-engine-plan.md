# The Ways — Story Engine: архитектурный план

Документ описывает целевую архитектуру подключаемого **Story Engine** для The Ways (Telegram-бот, игра дня на голосовании с блокчейн-экономикой). План опирается на актуальное состояние репозитория (commit `be7697c`, ветка `main`).

---

## 0. Исходные принципы

1. **Game Engine — постоянный.** КАК работает игра: голосование, закон, ставки, выплаты, лидерборд, планировщик — не переписывается.
2. **Story Engine — постоянный.** ЧТО сейчас происходит: сезон, глава, состояние истории, выборы, последствия.
3. **Story Pack — заменяемый.** КАКУЮ историю рассказываем: JSON-пак, подключаемый без изменения кода.

Жёсткие ограничения:

- Story Engine **не** определяет закон, **не** считает голоса, **не** выбирает победителя, **не** трогает деньги и кошелёк.
- Story Engine **не** импортирует `ton_watch`, `ton_pay`, `stakes`, `wallet`, `aiogram`, `aiogram.types`.
- Никакого runtime-LLM: контент статический. AI допустим только при создании контента.
- Экономика (96/1/2/0.5/0.5) остаётся как есть.
- Не создавать второго Telegram-бота и микросервиса на этом этапе.
- Никакого массового рефакторинга существующего Game Engine.

---

## 1. Текущее состояние (аудит)

### Поток дня

```
ensure_current_round → create_next_round_detailed → _plan_and_render → _materialize_round
→ close_voting → finish_tally → award_points → finalize_day_payouts
→ _announce_results_job → _finalize_new_day_job → announce_new_day
```

### Ключевые файлы и точки стыковки

| Файл | Строка | Что делает | Стыковка |
|---|---|---|---|
| `rounds/rendering.py` | `_plan_and_render` | Шаблонный payload: title, text, 3 карты, закон | Замена на Chapter из Story Engine |
| `rounds/lifecycle.py` | `create_next_round_detailed` | Создание Round + Cards | Добавить story_pack_id/season_id/story_day |
| `rounds/lifecycle.py` | `finish_tally` (hook) | Пишет StoryBeat | Добавить `apply_result` |
| `rounds/lifecycle.py` | `reset_game` | Сброс; keep_story сохраняет StoryBeat | Story state тоже сохранять при keep_story |
| `rounds/anchor.py` | `get_run_anchor` | Якорь забега «YYYY-MM» | Привязка season_id |
| `broadcast.py` | `cards_keyboard` | Кнопки «Путь I/II/III» | Названия из choices |
| `broadcast.py` | `status_text` | Текст дня: правило + карты | Story text → chapter text |
| `broadcast.py` | `build_day_post` | `[]` (медиа отключено) | Вернуть media из pack |
| `tally.py` | `format_plugin_results` | `""` — готовый хук | Строка story consequence |
| `leaderboard.py` | `is_last_day_of_month` | Граница месяца | Граница сезона |
| `leaderboard.py` | `mark_month_leaderboard_ready` | Флаг конца месяца | Точка переключения пака |
| `scheduler.py` | `tick` | Основной цикл | Без изменений |
| `handlers/admin.py` | Админ-команды | /advance, /resetgame | Добавить /story* |
| `config.py` | Settings | WORLD_NAME, LLM-ключи | Добавить STORY_PACKS_DIR |

### Что уже есть и НЕ трогаем

- Голосование, ставки, выплаты, лидерборд, watcher, пауза, бэкапы, health, /change, /wallet, disputes, streaks.
- WinRule (majority/minority/median) — публичен с минуты открытия (commit/reveal и sealed **удалены** ранее).
- StoryBeat модель (канон, unique по day_index).
- style.py (детерминированные марки по SHA-256).
- Месяц уже календарный: `is_last_day_of_month`, ключ «YYYY-MM» (`LeaderboardPot.month`, якорь забега).

---

## 2. Целевая архитектура

```
┌─────────────────────────────────────┐
│         GAME ENGINE (unchanged)     │
│  law / votes / stakes / payouts     │
│  leaderboard / scheduler / admin    │
└──────────────────┬──────────────────┘
                   │ StoryResult(law, winner, counts)
                   ▼
┌─────────────────────────────────────┐
│         STORY ENGINE                │
│  state / conditions / effects       │
│  chapter generation / renderer      │
│  idempotency / history              │
└──────────────────┬──────────────────┘
                   │ Chapter(title, text, choices, media)
                   ▼
┌─────────────────────────────────────┐
│         STORY PACK (JSON)           │
│  manifest / chapters / characters   │
│  lore / endings / state template    │
└─────────────────────────────────────┘
```

Game Engine передаёт Story Engine закон, победителя и распределение голосов. Story Engine решает, что произошло в истории. Telegram-рендеринг остаётся в существующем слое (Story Engine возвращает данные, а не шлёт сообщения).

---

## 3. Новый пакет `app/story/`

```
app/story/
    __init__.py          — публичный API (StoryEngine, load_pack)
    models.py            — dataclasses (чистый Python, без ORM)
    loader.py            — загрузка JSON-паков с диска + валидация
    state.py             — работа с StoryState (метрики, флаги)
    conditions.py        — evaluate conditions dict против state
    effects.py           — apply effects dict к state (guard против double-apply)
    engine.py            — StoryEngine: get_chapter(), apply_result(), get_ending()
    renderer.py          — Chapter → payload dict (совместимый с _plan_and_render)
    calendar.py          — real mesyachnaja dlina (calendar import, без фокусов) [опц.]
    validate.py          — CLI: python -m app.story.validate [pack_id]
```

**Разрешённые зависимости:** только `app.models.Round`, `app.models.WinRule` (для type hints), stdlib. Ни Telegram, ни финансов.

---

## 4. Data Models (`app/story/models.py`)

```python
@dataclass
class StoryManifest:
    id: str                    # "echo_of_the_pack"
    version: str               # "1.0.0"
    title: str                 # "Эхо Стаи"
    language: str              # "ru"
    max_season_days: int       # 31 (максимум глав; валидатор: 28..31)
    enabled: bool              # True
    state_template: dict       # начальное состояние сезона

@dataclass
class Choice:
    id: str                    # "A", "B", "C"
    title: str                 # "Войти в тоннель"
    description: str           # "Тёмный проход вглубь..."
    effects: dict[str, int]    # {"signal_level": 1, "observer_awareness": 1}
    conditions: dict | None    # {"observer_awareness": ">=5"} (показывается только при выполнении)

@dataclass
class Chapter:
    day: int                   # 1..season_length
    title: str                 # "Первый контакт"
    text: str                  # повествование (Telegram-friendly, ~500 знаков)
    choices: list[Choice]      # ровно 3 выбора
    npcs: list[str] | None     # ["vera", "rex"]
    lore: str | None           # одноразовый текст лора
    media_path: str | None     # путь к картинке (опционально)

@dataclass
class Ending:
    id: str                    # "THE_PACK", "THE_FRACTURE" и т.д.
    title: str
    text: str
    conditions: dict[str, str] # условия срабатывания финала

@dataclass
class NPC:
    id: str                    # "keeper", "vera", "rex", "null", "observer"
    name: str
    description: str
    role: str

@dataclass
class StoryState:
    pack_id: str
    season_id: str             # f"{pack}:{year}-{month:02d}"
    metrics: dict[str, int]
    flags: dict[str, bool]
    day: int                   # текущий день сезона (1..season_length)

@dataclass
class StoryResult:
    law: str                   # "majority"/"minority"/"median"
    winner: int                # 0, 1, 2 (позиция карты)
    distribution: dict[int, int]  # {0: 14, 1: 62, 2: 31}

@dataclass
class StoryConsequence:
    choice_id: str             # "A", "B", "C"
    effects_applied: dict[str, int]
    narrative_text: str        # "Стая выбрала тоннель. Сигнал усиливается..."
    state_after: StoryState    # снимок после эффектов
```

---

## 5. Длина сезона = календарный месяц

Месяц в коде уже календарный: `is_last_day_of_month` (`leaderboard.py:503`) и якорь забега `key=YYYY-MM` (`anchor.py:32`). Сезон привязан к нему же.

```python
def season_length(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]   # 28..31
```

- **Сезон = календарный месяц**: `season_id = f"{pack}:{year}-{month:02d}"`, согласован с `RUN_START_KEY`/`anchor` и `LeaderboardPot.month`.
- Контент — **максимум 31 глава** (`day_001.json … day_031.json`). Если в месяце 28/30 дней, главы 29–31 **не запрашиваются**: `get_chapter()` отсекает `day > season_length`.
- **Последний день сезона** определяется существующим предикатом `is_last_day_of_month(opens_at)` — единая граница для копилки месяца и финала сезона.
- **Переключение пака** (`active=pending`) выполняется при пересечении месяца, в той же точке, где `mark_month_leaderboard_ready` (`leaderboard.py:545`), а не по номеру дня.
- **Финал** выбирается по Story State именно на последний день месяца.
- Отсутствующая глава дня > длины месяца не запрашивается вовсе (fallback на шаблон не нужен — день не существует).

---

## 6. Loader (`app/story/loader.py`)

```
story_packs/
    echo_of_the_pack/
        manifest.json
        chapters/
            day_001.json .. day_031.json
        characters/
            keeper.json, vera.json, rex.json, null.json, observer.json
        lore/
            lore_001.json .. (опционально)
        endings/
            the_pack.json, the_fracture.json, the_mirror.json, ...
```

**manifest.json:**

```json
{
    "id": "echo_of_the_pack",
    "version": "1.0.0",
    "title": "Эхо Стаи",
    "language": "ru",
    "max_season_days": 31,
    "enabled": true,
    "state_template": {
        "metrics": {
            "signal_level": 0,
            "observer_awareness": 0,
            "pack_unity": 0,
            "trust_vera": 0,
            "trust_rex": 0,
            "trust_null": 0
        },
        "flags": {}
    }
}
```

**day_001.json:**

```json
{
    "day": 1,
    "title": "Сигнал",
    "text": "На экране терминала мелькнула помеха. Не шум — закономерность.",
    "choices": [
        {"id": "A", "title": "Разобрать сигнал", "description": "Проследить источник...",
         "effects": {"signal_level": 1, "observer_awareness": 1}, "conditions": null},
        {"id": "B", "title": "Игнорировать", "description": "Считать шумом...",
         "effects": {"pack_unity": -1}, "conditions": null},
        {"id": "C", "title": "Предупредить стаю", "description": "Рассказать другим...",
         "effects": {"trust_vera": 1, "signal_level": 1}, "conditions": null}
    ],
    "npcs": ["vera"],
    "lore": null,
    "media_path": null
}
```

**Loader API:**

```python
def load_pack(pack_dir: Path) -> StoryManifest
def load_chapter(pack_dir: Path, day: int) -> Chapter
def load_all_chapters(pack_dir: Path) -> list[Chapter]
def load_npc(pack_dir: Path, npc_id: str) -> NPC
def load_endings(pack_dir: Path) -> list[Ending]
def list_packs(base_dir: Path) -> list[str]
```

Все файлы — UTF-8 JSON. Валидация при загрузке (см. §10).

---

## 7. Engine (`app/story/engine.py`)

```python
class StoryEngine:
    def __init__(self, packs_dir: Path = Path("story_packs")):
        self._packs_dir = packs_dir
        self._cache: dict[str, StoryManifest] = {}

    async def get_active_pack(self, session: AsyncSession) -> StoryPackInfo | None:
        """Читает из watcher_state: story:active_pack, story:pending_pack."""

    async def set_pending_pack(self, session: AsyncSession, pack_id: str) -> None:
        """Устанавливает pending_pack."""

    async def advance_pack_if_needed(self, session: AsyncSession, finished_round: Round) -> None:
        """На границе месяца: active=pending, pending=null."""

    async def get_chapter(
        self, pack_id: str, season_id: str, day: int,
        state: StoryState, law: str,
    ) -> Chapter | None:
        """Возвращает главу; если pack не найден или day > season_length — None
        (Game Engine рендерит шаблон)."""

    async def apply_result(
        self, session: AsyncSession,
        pack_id: str, season_id: str, day: int,
        result: StoryResult, state: StoryState,
    ) -> StoryConsequence:
        """Идемпотентно: если день уже обработан — вернёт сохранённый.
        Эффекты применяются к state, state сохраняется в story_states,
        история — в story_history. НЕ трогает Payout/Round/Vote."""

    async def get_state(self, session: AsyncSession, pack_id: str, season_id: str) -> StoryState:
        """Загружает состояние из story_states; если нет — создаёт из template."""

    async def get_ending(self, pack_id: str, state: StoryState) -> Ending | None:
        """Определяет финал по conditions."""

    def validate_pack(self, pack_id: str) -> list[str]:
        """Полная валидация пака (для CLI validator)."""
```

**Идемпотентность:**

- `apply_result` проверяет `story_history` по `(season_id, day)`.
- Если запись есть — возвращает сохранённый `StoryConsequence`.
- Если нет — применяет эффекты, сохраняет state, пишет history.
- Внутри одной транзакции: SELECT FOR UPDATE на story_states → apply → INSERT history → UPDATE state.

---

## 8. State Management (`app/story/state.py`)

`story_states`:

```python
class StoryStateRow(Base):
    __tablename__ = "story_states"
    id: Mapped[int] = mapped_column(primary_key=True)
    pack_id: Mapped[str] = mapped_column(String(64))
    season_id: Mapped[str] = mapped_column(String(64))
    metrics: Mapped[str] = mapped_column(Text)  # JSON
    flags: Mapped[str] = mapped_column(Text)     # JSON
    day: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = ...
    updated_at: Mapped[datetime] = ...
    __table_args__ = (UniqueConstraint("pack_id", "season_id"),)
```

Active/pending pack — в `watcher_state`:

- `story:active_pack` → `"echo_of_the_pack"`
- `story:pending_pack` → `"the_bazaar"` или `""`

---

## 9. History & Idempotency Guard

```python
class StoryHistoryRow(Base):
    __tablename__ = "story_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    season_id: Mapped[str] = mapped_column(String(64))
    day_index: Mapped[int] = mapped_column(Integer)
    winning_choice: Mapped[str] = mapped_column(String(4))   # "A"/"B"/"C"
    effects_json: Mapped[str] = mapped_column(Text)          # JSON applied effects
    story_consequence_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = ...
    __table_args__ = (UniqueConstraint("season_id", "day_index"),)
```

---

## 10. Conditions (`app/story/conditions.py`)

```python
# conditions: {"observer_awareness": ">=5", "trust_vera": ">0"}
OPERATORS = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
}

def evaluate(conditions: dict[str, str], state: StoryState) -> bool:
    """Все условия должны выполняться (AND)."""
```

---

## 11. Effects (`app/story/effects.py`)

```python
def apply_effects(state: StoryState, effects: dict[str, int]) -> StoryState:
    """Возвращает НОВЫЙ state с применёнными эффектами. Не мутирует оригинал.
    Ключи с префиксом flag: меняют булевы флаги."""
    new_metrics = {**state.metrics}
    for key, delta in effects.items():
        if key.startswith("flag:"):
            new_flags = {**state.flags}
            new_flags[key[5:]] = bool(delta)
            return replace(state, flags=new_flags, metrics=new_metrics)
        new_metrics[key] = new_metrics.get(key, 0) + delta
    return replace(state, metrics=new_metrics)
```

---

## 12. Интеграция в Game Engine

### a) Открытие дня — `app/rounds/rendering.py:49` `_plan_and_render`

```python
async def _plan_and_render(session, day_index, opens_hint=None):
    from app.story.engine import StoryEngine
    se = StoryEngine()
    pack_info = await se.get_active_pack(session)
    if pack_info and pack_info.enabled:
        state = await se.get_state(session, pack_info.pack_id, pack_info.season_id)
        rule = rng.choice(list(WinRule))
        chapter = await se.get_chapter(
            pack_info.pack_id, pack_info.season_id, day_index, state, rule.value
        )
        if chapter:
            return render_story_chapter(chapter, day_index, rule, pack_info)  # → payload dict
    # fallback: шаблонный день
    ...
```

### b) Round creation — `app/rounds/materialization.py:23` `_materialize_round`

```python
round_row = Round(
    ...,
    story_pack_id=payload.get("story_pack_id"),
    season_id=payload.get("season_id"),
    story_day=payload.get("story_day"),
)
```

### c) Закрытие — `app/rounds/lifecycle.py:358` `finish_tally`

```python
# После StoryBeat (hook):
from app.story.engine import StoryEngine
se = StoryEngine()
pack_info = await se.get_active_pack(session)
if pack_info:
    state = await se.get_state(session, pack_info.pack_id, pack_info.season_id)
    result = StoryResult(law=round_row.win_rule.value, winner=winner, distribution=counts)
    consequence = await se.apply_result(
        session, pack_info.pack_id, pack_info.season_id,
        round_row.day_index, result, state,
    )
    round_row.story_consequence = consequence.narrative_text  # новая колонка
```

### d) Итоги — `app/tally.py:183` `format_plugin_results`

```python
async def format_plugin_results(round_row, session=None):
    if getattr(round_row, "story_consequence", ""):
        return round_row.story_consequence
    return ""
```

### e) Кнопки — `app/broadcast.py:28` `cards_keyboard`

Уже универсален (`callback_data=f"vote:{round_id}:{pos}"»). Текст кнопок берётся из `card.title`, который приходит из payload — если payload пришёл от Story Engine, кнопки автоматически покажут названия choices.

### f) Reset — `app/rounds/lifecycle.py:121` `reset_game`

- `story_history` НЕ удаляется при `keep_story=True` (канон, как PackFundLedger).
- `story_states` удаляются только при полном сбросе (`keep_story=False`).

### g) Админ — `app/handlers/admin.py`

- `/story` — текущий pack, сезон, день, состояние (без публичных метрик).
- `/story_preview <day>` — preview главы (только админ).
- `/story_state` — текущее состояние (метрики, флаги).
- `/story_switch <pack_id>` — назначить pack на следующий сезон.

---

## 13. Новые колонки Round

```python
story_pack_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
season_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
story_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
story_consequence: Mapped[str | None] = mapped_column(Text, nullable=True)
```

Nullable=True — совместимость с историческими раундами без истории.

---

## 14. Alembic Migration (одна миграция)

```sql
-- story_states
CREATE TABLE story_states (...);
-- story_history
CREATE TABLE story_history (...);
-- rounds: новые колонки
ALTER TABLE rounds ADD COLUMN story_pack_id VARCHAR(64);
ALTER TABLE rounds ADD COLUMN season_id VARCHAR(64);
ALTER TABLE rounds ADD COLUMN story_day INTEGER;
ALTER TABLE rounds ADD COLUMN story_consequence TEXT;
-- story_beats остаётся как есть (канон)
```

---

## 15. Validator CLI (`app/story/validate.py`)

```
python -m app.story.validate                    # все паки
python -m app.story.validate echo_of_the_pack   # конкретный
```

Проверяет:

- manifest валиден, все обязательные поля присутствуют.
- `max_season_days` ∈ [28, 31] — покрывает все возможные месяцы, включая февраль.
- Главы пронумерованы подряд от 1 (нет «дыр» на 28..31).
- Количество глав не превышает `max_season_days`, но ≥ 28.
- Каждый день имеет ровно 3 выбора с уникальными id.
- Effects ключи известны (без опечаток в названиях метрик).
- Conditions синтаксически валидны.
- Ссылки на NPC существуют.
- Endings условия валидны; ≥ 1 финал как fallback (для месяца, оборванного на 28-м дне).
- Нет orphan-ссылок, season_length согласован.

---

## 16. Simulator (`app/simulate.py`)

```
python -m app.simulate --days 3 --skip-images
python -m app.simulate --story echo_of_the_pack --days 30
```

Симуляция: загружает пак, прогоняет N дней, печатает chapter title/text/choices/winner/consequence. Не требует Telegram. Существующий CLI не ломаем (README уже упоминает `python -m app.simulate --days N --skip-images`, модуля пока нет — создаём).

---

## 17. Season 001 «Эхо Стаи» — содержание

**Акты:**

| Act | Дни | Тема | Метрики |
|---|---|---|---|
| I SIGNAL | 1-7 | Первый контакт | signal_level ↑, observer_awareness ↑ |
| II NOISE | 8-14 | Противоречивые сообщения | pack_unity ↔, trust_vera ↔ trust_rex ↔ |
| III PACK | 15-21 | Стая осознаёт наблюдение | observer_awareness ↑↑ |
| IV OBSERVER | 22-27 | Поиск Наблюдателя | flags: observer_revealed |
| V ECHO | 28-31 | Архив решений | Определяет финал |

**NPC (§39):** keeper, vera, rex, null, observer.

**Финалы:** THE_PACK, THE_FRACTURE, THE_MIRROR, THE_ESCAPE, THE_LOOP.

**Условия финалов (пример):**

```python
{
    "THE_PACK":     {"pack_unity": ">=8", "observer_awareness": "<5"},
    "THE_FRACTURE": {"pack_unity": "<=2"},
    "THE_MIRROR":   {"observer_awareness": ">=8", "trust_null": ">=3"},
    "THE_ESCAPE":   {"signal_level": "<=2", "pack_unity": ">=5"},
    "THE_LOOP":     {}  # default fallback
}
```

---

## 18. Тесты (`tests/test_story_*.py`)

| Тест | Что проверяет |
|---|---|
| `test_story_loader` | Загрузка manifest, chapters, NPCs, endings |
| `test_story_validator` | CLI validator проходит на валидном паке |
| `test_story_engine` | get_chapter возвращает Chapter; apply_result идемпотентен |
| `test_story_state` | Загрузка/сохранение состояния, create from template |
| `test_story_effects` | apply_effects корректно меняет metrics/flags |
| `test_story_conditions` | evaluate для всех операторов |
| `test_story_calendar` | Длина сезона 28/29/30/31; отсечение глав за границей месяца |
| `test_story_switch` | /story_switch, advance_pack_if_needed |
| `test_story_finale` | Определение финала по conditions |
| `test_story_integration_round` | Round со story_pack_id создаётся, chapter подключается |
| `test_story_integration_winner` | Story consequence в format_plugin_results |
| `test_story_integration_reset` | keep_story сохраняет story state |
| `test_story_integration_restart` | story_state переживает рестарт |

Плюс регрессия: существующие 365 тестов не должны упасть.

---

## 19. Фазы реализации

| Фаза | Что | Зависит от |
|---|---|---|
| 1. Core | `app/story/{models,loader,state,conditions,effects,engine,renderer,calendar}.py` | — |
| 2. Schema | Alembic migration (story_states, story_history, Round columns) | Фаза 1 |
| 3. Integration | rendering, lifecycle, tally, broadcast, handlers/admin | Фазы 1-2 |
| 4. Pack content | `story_packs/echo_of_the_pack/` (до 31 главы, NPC, финалы) | Фаза 1 |
| 5. Validator + Simulator | `validate.py`, `simulate.py` | Фазы 1, 4 |
| 6. Tests | Все test_story_* + регрессия | Фазы 1-3 |

---

## 20. Риски и стратегии

| Риск | Стратегия |
|---|---|
| 31 день контента — большой объём | JSON-формат; пишем поэтапно; fallback на шаблон, если pack не найден |
| State теряется при рестарте | DB-persistent (story_states), не in-memory |
| Story Engine ломает Game Engine | Полная изоляция: story* импортирует только Round/WinRule; вызовы как plugin |
| Double-apply эффектов | Unique(season_id, day_index) в story_history + SELECT FOR UPDATE |
| Смена pack посреди сезона | Pack привязан к Round через snapshot; смена только на границе месяца |
| Длинный/короткий месяц | Контент на 31 день; игра заканчивается на день `calendar.monthrange()[1]`; главы за границей не запрашиваются |

---

## 21. Критерии готовности

Story Engine считается реализованным, только если:

- существующий Game Engine изменён только в точках стыковки (§12);
- payout, старые тесты, reset, /change, /health, бэкапы работают;
- текущий round хранит снимок story_pack_id/season_id/story_day;
- рестарт процесса не теряет Story State;
- Story Pack можно заменить командой /story_switch (на границе месяца);
- новый сюжет не требует изменений в blockchain/Telegram-коде;
- consequence не применится дважды;
- сюжет не зависит от LLM в runtime.