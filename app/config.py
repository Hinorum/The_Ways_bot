from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic_settings import BaseSettings, SettingsConfigDict


LIBPQ_QUERY_KEYS = {
    "sslmode",
    "channel_binding",
    "gssencmode",
    "target_session_attrs",
}


def sqlalchemy_url(url: str) -> str:
    raw = url.strip()
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://") :]
    if raw.startswith("postgresql://") and "+asyncpg" not in raw:
        raw = "postgresql+asyncpg://" + raw[len("postgresql://") :]
    parsed = urlparse(raw)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    kept = [(key, value) for key, value in pairs if key.lower() not in LIBPQ_QUERY_KEYS]
    # Не пересобираем URL без изменений: urlunparse теряет «//» у схем без netloc (например, sqlite).
    if len(kept) == len(pairs):
        return raw
    return urlunparse(parsed._replace(query=urlencode(kept)))


def postgres_connect_args(url: str) -> dict:
    converted = sqlalchemy_url(url)
    if not converted.startswith("postgresql"):
        return {}
    host = urlparse(converted).hostname or ""
    if host in {"localhost", "127.0.0.1"}:
        return {}
    return {"ssl": True}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str = ""
    admin_ids: str = ""
    round_seconds: int = 82_800
    tally_seconds: int = 3_600
    # Сетка расписания (UTC): новый день открывается в day_open_hour_utc,
    # голосование идёт до (day_open_hour_utc - 1):00 следующего дня,
    # час подсчёта — и сразу после него итоги вместе с новым днём.
    day_open_hour_utc: int = 11
    database_url: str = "sqlite+aiosqlite:///./data/the_way.db"
    timezone: str = "Europe/Moscow"
    media_dir: str = "./media/generated"
    use_free_images: bool = True
    # Токен Pollinations (pollinations.ai → auth): поднимает лимиты анонимного
    # tier'а — без него общий IP Render регулярно ловит 429 на весь день.
    pollinations_token: str = ""
    use_free_story_llm: bool = True
    # Бесплатные модели неторопливы: таймауты щедрые, чтобы день собирался
    # нейросетью, а не фолбэками. Настраивается из Environment.
    llm_timeout_seconds: int = 75
    image_timeout_seconds: int = 90
    story_models: str = "openai-fast,openai,mistral"
    llm_api_key: str = ""
    llm_base_url: str = "https://router.huggingface.co/v1/chat/completions"
    llm_models: str = "meta-llama/Llama-3.3-70B-Instruct"

    ton_enabled: bool = False
    ton_network: str = "mainnet"
    treasury_address: str = ""
    treasury_mnemonic: str = ""
    treasury_testnet_address: str = ""
    treasury_testnet_mnemonic: str = ""
    ton_api_base: str = "https://tonapi.io"
    ton_api_base_testnet: str = "https://testnet.tonapi.io"
    ton_api_key: str = ""
    # Версия контракта казначея: auto (детект по адресу), v4r2 или v5r1.
    treasury_wallet_version: str = "auto"
    # Свежий JSON-конфиг лайтсерверов для pytoniq (ADNL/UDP). Встроенный
    # конфиг тестнета периодически мёртв («have no alive peers»): сюда
    # подставляется рабочий URL, например официальный
    # https://ton.org/testnet-global.config.json
    liteserver_config_url: str = ""
    # Резервный источник истории переводов (Toncenter API v3): включается
    # автоматически, когда TonAPI лжёт (404 истории при живом кошельке) или лежит.
    toncenter_api_base: str = "https://toncenter.com"
    toncenter_api_base_testnet: str = "https://testnet.toncenter.com"
    toncenter_api_key: str = ""
    # Нижняя граница ставки; верхней нет — «кит» ограничен только своим кошельком.
    stake_min_ton: float = 0.1
    # Распределение фонда дня (в сумме со ставками победителей — 100%):
    # 97% делят поставившие на верный путь пропорционально, 2% капают в
    # копилку недели (в понедельник её делят топ-3 по верным ответам),
    # 0,5% — хранителю, 0,5% — в копилку месяца (/top). Проценты в долях.
    owner_rake_pct: float = 0.5
    leaderboard_rake_pct: float = 0.5
    weekly_pot_pct: float = 2.0
    # Недельный лидерборд: минимальное число дней голосования за неделю,
    # чтобы претендовать на призовое место (анти-мультиаккаунт), и доли
    # мест «1-е,2-е,3-е» в процентах.
    weekly_min_days: int = 4
    weekly_prize_pcts: str = "20,30,50"
    owner_wallet_address: str = ""
    stake_confirm_seconds: int = 40
    # Столько раз зависшая выплата ретраится, прежде чем окончательно встать в failed.
    payout_max_attempts: int = 5
    # Потолок одной попытки вещания перевода (сек): зависший лайтсервер не
    # имеет права замораживать весь цикл выплат — таймаут = обычный ретрай.
    payout_send_timeout_seconds: int = 90
    # Глубина скана входящих переводов казначея за один цикл наблюдателя:
    # до watch_max_pages страниц по watch_page_limit транзакций. Курсор делает
    # покрытие кумулятивным — после простоя хвост догоняется за пару циклов.
    watch_page_limit: int = 100
    watch_max_pages: int = 50
    # Авто-возврат только свежим переводам: после сброса базы курсор watcher'а
    # обнуляется и вся история казны перечитывается заново — без этого лимита
    # старый спам-хлам вечно превращается в новые dead-letter возвраты.
    watch_refund_max_age_days: int = 14
    # Грубый стоп-фильтр генераций перед постингом в чаты.
    content_filter: bool = True

    # Час вечерней микросцены («вечерний привал») в UTC: короткая сцена между
    # утренней главой и закрытием голосования. 16:00 UTC = 19:00 Москвы.
    whisper_hour_utc: int = 16

    # Личное эхо проигравшим: после итогов каждый, кто голосовал мимо
    # победившего пути, получает в личку короткое «чем пахла бы его тропа».
    personal_echo: bool = True

    # Мир игры. Название попадает на картинки и в тексты бота,
    # brief — в системный промпт нейросети. Можно поменять из Environment,
    # не трогая код: получится другой мир с той же механикой.
    world_name: str = "Эхо Стаи"
    world_brief: str = (
        "Фанатская история по мотивам Lost Dogs, не связанная с официальной командой. "
        "После Последнего Пути Стая потерянных собак нашла сеть нестабильных миров-порталов: "
        "каждый мир собран из чужих решений и воспоминаний. Порталы глючат и путают ветки "
        "реальности, Архивариусы хранят спорные версии событий, мутанты слышат то, чего ещё "
        "не было, а где-то в глубине сети звучит Первый Лай — зов домой или ловушка, не знает никто."
    )

    revote_enabled: bool = True
    revote_stars: int = 25
    revote_ton: float = 0.1
    # Глухой день: раз в N дней закон не объявляется утром — публикуется только
    # хеш-обязательство, а сам закон вскрывается в итогах. 0 — выключить.
    sealed_day_every: int = 10
    # Период само-пинга /health: держит free plan Render от засыпания, чтобы
    # день открывался по UTC-сетке. 0 — выключить.
    self_ping_seconds: int = 600
    port: int = 10000
    webhook_base_url: str = ""
    webhook_secret: str = ""
    render_external_url: str = ""
    render_external_hostname: str = ""

    @property
    def admin_id_set(self) -> set[int]:
        ids: set[int] = set()
        for part in self.admin_ids.replace(" ", "").split(","):
            if part.isdigit():
                ids.add(int(part))
        return ids

    @property
    def story_model_chain(self) -> list[str]:
        models = [model.strip() for model in self.story_models.split(",") if model.strip()]
        return models or ["openai"]

    @property
    def llm_model_chain(self) -> list[str]:
        models = [model.strip() for model in self.llm_models.split(",") if model.strip()]
        return models or ["meta-llama/Llama-3.3-70B-Instruct"]

    @property
    def async_database_url(self) -> str:
        return sqlalchemy_url(self.database_url)

    @property
    def public_base_url(self) -> str:
        if self.webhook_base_url:
            return self.webhook_base_url.rstrip("/")
        if self.render_external_url:
            return self.render_external_url.rstrip("/")
        if self.render_external_hostname:
            return f"https://{self.render_external_hostname.rstrip('/')}"
        return ""

    @property
    def use_webhook(self) -> bool:
        return bool(self.public_base_url)

    @property
    def is_testnet(self) -> bool:
        return self.ton_network.strip().lower() == "testnet"

    @property
    def active_treasury_address(self) -> str:
        return self.treasury_testnet_address if self.is_testnet else self.treasury_address

    @property
    def active_treasury_mnemonic(self) -> str:
        return self.treasury_testnet_mnemonic if self.is_testnet else self.treasury_mnemonic

    @property
    def active_ton_api_base(self) -> str:
        return self.ton_api_base_testnet if self.is_testnet else self.ton_api_base

    @property
    def active_toncenter_api_base(self) -> str:
        return self.toncenter_api_base_testnet if self.is_testnet else self.toncenter_api_base


settings = Settings()
