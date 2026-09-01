from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class RoundStatus(str, enum.Enum):
    OPEN = "open"
    TALLYING = "tallying"
    CLOSED = "closed"


class WinRule(str, enum.Enum):
    MAJORITY = "majority"
    MINORITY = "minority"
    MEDIAN = "median"


RULE_PHRASES = {
    WinRule.MAJORITY: "побеждает карта, собравшая больше всех голосов",
    WinRule.MINORITY: "побеждает карта, собравшая меньше всех голосов",
    WinRule.MEDIAN: "побеждает карта со средним числом голосов",
}

# Маски дня: существа бестиария, в чью ночь выпадает закон. Механику не
# меняют — это нарративная линза для тизера и бестиария.
RULE_MASKS = {
    WinRule.MAJORITY: ("Голос Стаи", "ночь, когда мир слушает хор"),
    WinRule.MINORITY: ("Одинокий Волк", "ночь, когда право пути у отставшего"),
    WinRule.MEDIAN: ("Середняк", "ночь существа, живущего между крайностями"),
}
SEALED_MASK = ("Слепая Яма", "день, где закон запечатан до темноты")


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    correct_picks: Mapped[int] = mapped_column(Integer, default=0)
    wallet_address: Mapped[str | None] = mapped_column(String(80), nullable=True, unique=True)
    wallet_linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Кошелёк подтверждён микро-переводом с него (мемо bv:<код>): привязка без
    # доказательства владения давала бы присваивать любой публичный адрес и
    # ловить на него чужие призы. Пока деньги включены, ставки с неподтверждённого
    # кошелька не считаются — сначала владелец доказывает контроль.
    wallet_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # Код подтверждения привязки: известен только владельцу телеграм-аккаунта,
    # отправить перевод с нужного адреса может только владелец кошелька — так
    # совпадение «адрес + код» доказывает контроль. Null — ждать нечего.
    wallet_verify_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    wallet_verify_created: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Призвание собаки (ключ из app.callings): косметика нарратива — титулы,
    # окраска личного эха и касания в главах. На деньги и вес голоса не влияет.
    calling: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Жетоны «Второго нюха»: за находки памяти и верные серии. Тратятся на
    # личную микросцену дня; информации о законе не дают.
    inspiration: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Подписка на личные дубликаты рассылок (итоги дня, новый день с обложкой,
    # вечерний пост и прочие анонсы) в личку бота. По умолчанию — да; игрок
    # может снять или вернуть её кнопкой в /start.
    dm_subscribed: Mapped[bool] = mapped_column(Boolean, default=True)
    # Стрик: текущая серия правильных голосований и лучшая за всё время.
    current_streak: Mapped[int] = mapped_column(Integer, default=0)
    best_streak: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Referral(Base):
    """Кто привёл кого по личной ссылке ?start=ref_<id>_<токен>.

    Строго одна запись на приведённого: первый валидный переход фиксируется
    навсегда, повторные /start с чужой ссылкой игнорируются. Сейчас тут только
    факт приведения — награды и анти-сибил-штрафы появятся позже.
    """

    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(BigInteger, index=True)
    referred_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Chat(Base):
    """Чаты (группы/каналы), где бот состоит и куда рассылаются анонсы дней."""

    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    type: Mapped[str] = mapped_column(String(32))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def _enum_values(enum_cls):
    return [member.value for member in enum_cls]


class Round(Base):
    __tablename__ = "rounds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day_index: Mapped[int] = mapped_column(Integer, unique=True)
    status: Mapped[RoundStatus] = mapped_column(
        Enum(RoundStatus, native_enum=False, values_callable=_enum_values),
        default=RoundStatus.OPEN,
    )
    win_rule: Mapped[WinRule] = mapped_column(
        Enum(WinRule, native_enum=False, values_callable=_enum_values)
    )
    rule_commitment: Mapped[str] = mapped_column(String(128))
    # Глухой день: закон запечатан до итогов, игрокам показан только хеш.
    sealed: Mapped[bool] = mapped_column(Boolean, default=False)
    chapter_title: Mapped[str] = mapped_column(String(300))
    chapter_text: Mapped[str] = mapped_column(Text)
    lore_summary: Mapped[str] = mapped_column(Text)
    cover_path: Mapped[str] = mapped_column(String(400), default="")
    opens_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    voting_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    tally_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    winner_card: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vote_counts_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Человеческое объяснение ничьей: «жребий закона по обязательству дня
    # выбрал путь II из II и III». Null — победа без равенства.
    tie_note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Момент первой успешной рассылки дня: повторный анонс того же дня
    # невозможен даже при гонке двух процессов после деплоя.
    announced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Сезон мира (календарный месяц «YYYY-MM» по UTC): финал сезона — День
    # Первого Лая последнего дня месяца; 1-го числа, после копилки лидеров,
    # начинается новый сезон.
    season: Mapped[str | None] = mapped_column(String(7), nullable=True, index=True)
    # Место действия дня: даёт сети память о географии — возвращение в место
    # показывает игрокам перемены от давних выборов.
    place: Mapped[str | None] = mapped_column(String(80), nullable=True)
    pot_nanotons: Mapped[int] = mapped_column(BigInteger, default=0)
    rake_nanotons: Mapped[int] = mapped_column(BigInteger, default=0)
    # Доля дня, ушедшая в копилку недели (2% фонда) — для поста итогов.
    weekly_nanotons: Mapped[int] = mapped_column(BigInteger, default=0)
    payouts_finalized: Mapped[bool] = mapped_column(Boolean, default=False)
    # Денежная версия дня (ставки TON + платная смена выбора): снимок режима
    # на момент открытия дня. Хранитель переключает «версию со ставками/без»
    # из /panel — новая версия вступает со СЛЕДУЮЩЕГО дня, а этот флаг
    # замораживает решение текущего дня (банк, /stake, /change).
    money_mode: Mapped[bool] = mapped_column(Boolean, default=True)
    # Эпилог дня от нейросети: чем отозвался победивший путь (пусто — не написан).
    epilogue_text: Mapped[str] = mapped_column(String(700), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    cards: Mapped[list[Card]] = relationship(back_populates="round", cascade="all, delete-orphan")


class Card(Base):
    __tablename__ = "cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text)
    image_path: Mapped[str] = mapped_column(String(400))
    consequence: Mapped[str] = mapped_column(Text)
    tag: Mapped[str] = mapped_column(String(16), default="care")

    round: Mapped[Round] = relationship(back_populates="cards")

    __table_args__ = (UniqueConstraint("round_id", "position", name="uq_card_round_pos"),)


class Vote(Base):
    """Append-only. Counts are never stored here during an open round."""

    __tablename__ = "votes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id"), index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    card_position: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("round_id", "player_id", name="uq_vote_round_player"),)


class StoryBeat(Base):
    __tablename__ = "story_beats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day_index: Mapped[int] = mapped_column(Integer, unique=True)
    winning_title: Mapped[str] = mapped_column(String(120))
    winning_text: Mapped[str] = mapped_column(Text)
    win_rule: Mapped[str] = mapped_column(String(32))
    vote_counts: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoreEcho(Base):
    """Отложенное последствие выбора: спит в каноне и всплывает через несколько дней.

    Каждый итог дня оставляет три эха (по одному на карту). Победившее — сильное
    (strength=3) и всплывает раньше; невыбранные пути тоже не исчезают, а ждут
    у края дороги. Сильное эхо при всплытии порождает цепочку «второго эха».
    """

    __tablename__ = "lore_echoes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    born_day: Mapped[int] = mapped_column(Integer, index=True)
    source_day: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text)
    strength: Mapped[int] = mapped_column(Integer, default=1)
    earliest_day: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(16), default="dormant")
    surfaced_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Stake(Base):
    """Ставка TON на путь в дне. Одна на игрока в раунде, идемпотентна по tx_hash.

    Жизненный цикл: pending (увидена в блокчейне) → confirmed (набрало
    «возраст» в блоках) → после итогов дня либо учтена в фонде победителям,
    либо refunded. rejected — нарушены лимиты/мемо, деньги отправляются назад.
    """

    __tablename__ = "stakes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id"), index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    amount_nanotons: Mapped[int] = mapped_column(BigInteger)
    tx_hash: Mapped[str] = mapped_column(String(80), unique=True)
    memo: Mapped[str] = mapped_column(String(64), default="")
    network: Mapped[str] = mapped_column(String(16), default="mainnet")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("round_id", "player_id", name="uq_stake_round_player"),)


class Payout(Base):
    """Исходящий перевод: приз, бонус угадавшему без ставки, возврат ставки,
    авто-возврат «ничейного» перевода или доля казны (рейк и копилка месяца).

    player_id и round_id nullable: у долей казны игрока нет, а переводы от
    неопознанных отправителей или в закрытый день не привязаны к раунду.
    """

    __tablename__ = "payouts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    round_id: Mapped[int | None] = mapped_column(ForeignKey("rounds.id"), nullable=True, index=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="prize")
    amount_nanotons: Mapped[int] = mapped_column(BigInteger)
    dest_address: Mapped[str] = mapped_column(String(80))
    tx_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    network: Mapped[str] = mapped_column(String(16), default="mainnet")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Причина последней неудачи отправки: видно в /payouts и алертах админу,
    # диагноз не требует раскопок логов сервиса.
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Свободный комментарий перевода вместо служебного memo «way:…»:
    # возвраты при паузе игры несут игроку текст о техработах, а не
    # технический идентификатор. Null — используется стандартное memo.
    comment_override: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Админ уже предупреждён об этой выплате: дедуп алертов живёт в БД,
    # а не в памяти процесса — переживает рестарт, безопасен при репликах.
    alerted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WatcherState(Base):
    """Служебные метки фоновых задач: ключ → значение.

    Здесь живут курсор наблюдателя TON, сердцебиения, план Хозяина Ошибки
    (список канонических событий сезона — длинные строки) и якорь арта.
    Value — Text без лимита: план злодея на 2-й ступени сезона переставал
    влезать в VARCHAR(255), роняя тик до создания следующего дня.
    """

    __tablename__ = "watcher_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LeaderboardPot(Base):
    """Копилка месяца: 0,5% фонда каждого дня капает сюда.

    В конце месяца сумма уходит игроку (игрокам) с максимумом верных
    ответов за месяц; месяц — ключ «YYYY-MM» по UTC-времени итогов дня.
    """

    __tablename__ = "leaderboard_pots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    month: Mapped[str] = mapped_column(String(7), unique=True)
    nanotons: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WeeklyPot(Base):
    """Копилка недели: 2% фонда каждого дня капает сюда.

    В понедельник сумма уходит топ-3 недели по числу верных ответов
    (места делят приз по WEEKLY_PRIZE_PCTS, по умолчанию 50/30/20%:
    сильнейший забирает больше); неделя — ISO-ключ «YYYY-Www» по UTC-времени
    открытия дня. Доле места без подходящего игрока (нет кошелька, мало дней
    голосования или нет ставки за неделю) ждать нечего — она переносится
    в копилку новой недели.
    """

    __tablename__ = "weekly_pots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    week: Mapped[str] = mapped_column(String(16), unique=True)
    nanotons: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LeaderboardClaim(Base):
    """Претензия на место лидерборда: решает ничьи по кнопке Claim в /start.

    Ничья в периоде решается по верности путей, затем по сумме ставок Gram,
    затем — кто раньше нажал Claim (кнопка видна в течение периода игрокам
    с кошельком и ставкой). Запись фиксирует момент претензии; unique-тройка
    (player_id, kind, period) делает Claim идемпотентным.
    """

    __tablename__ = "leaderboard_claims"
    __table_args__ = (
        UniqueConstraint("player_id", "kind", "period", name="uq_leaderboard_claims_player_kind_period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)  # "week" | "month"
    period: Mapped[str] = mapped_column(String(16), nullable=False)  # "YYYY-Www" | "YYYY-MM"
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PackFund(Base):
    """Фонд Стаи: 1% банка каждого дня капает сюда и не раздаётся сам.

    В отличие от копилок недели (WeeklyPot) и месяца (LeaderboardPot) у фонда нет
    периода авто-розыгрыша: это неубывающее накопление-обязательство. Хранитель
    видит баланс в пульте и распоряжается им вручную (например, разыгрывает
    среди любой группы или тратит на приз), выводя деньги обычным ручным путём.
    """

    __tablename__ = "pack_fund"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nanotons: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PackFundLedger(Base):
    """Прозрачный журнал Фонда Стаи: каждое поступление и ручная раздача.

    PackFund — это накопление-обязательство без периода авто-розыгрыша: 1% банка
    дня капает сюда, а Хранитель распоряжается деньгами вручную с казначейского
    кошелька. Сами физические переводы делает обычная очередь выплат, поэтому
    журнал здесь — аудит-след: «in» пишется при каждом начислении дня, «out» —
    ручной операцией хранителя (см. /fundout), чтобы стая видела, куда уходят
    накопленные граммы, а не одна непрозрачная цифра баланса.
    """

    __tablename__ = "pack_fund_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_type: Mapped[str] = mapped_column(String(8), default="in")  # in | out
    amount_nanotons: Mapped[int] = mapped_column(BigInteger)
    # Намеренно БЕЗ ForeignKey на rounds: журнал — append-only аудит, который
    # живёт дольше истории дней и не должен подпадать под сброс reset_game.
    # round_id — просто номер дня-источника (вместе с note хранит и день).
    round_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Dispute(Base):
    """Формальная жалоба игрока на итог дня, рассматриваемая хранителем.

    Каркас разрешения споров без авто-эскроу: сам исход дня НЕ отменяется
    и не переворачивается задним числом (выплаты победителям не трогаются),
    а рассматривается как претензия — результат фиксируется резолюцией
    (resolved/rejected). Компенсация при подтверждённой претензии — это
    обычная выплата kind="dispute" в общей очереди выплат, сделанная вручную
    хранителем после ревью. Так реальные деньги двигаются только по
    выстраданному пути, а спор остаётся подконтрольным аудитом.
    """

    __tablename__ = "disputes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    round_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    player_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    reason: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|resolved|rejected
    keeper_note: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RevoteGrant(Base):
    """Оплаченное право изменить свой выбор в дне (Stars или TON).

    unit_ref — идемпотентность платежа: telegram_payment_charge_id для Stars,
    хеш транзакции для TON. granted → used в момент фактической смены пути.
    round_id nullable — так записываются «осиротевшие» оплаты за закрытый день
    (возврат таких — ручная операция хранителя).
    """

    __tablename__ = "revote_grants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    round_id: Mapped[int | None] = mapped_column(ForeignKey("rounds.id"), nullable=True, index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    source: Mapped[str] = mapped_column(String(16), default="stars")
    unit_ref: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="granted")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Income(Base):
    """Поступление в казну: ledger для сверки с балансом бота и кошелька.

    kind="stars" — Telegram Stars за смену пути (amount_stars), выводятся
    владельцем через Fragment; kind="ton" — прямые переводы в казну
    (amount_nanotons); network помечает TON-строки (mainnet/testnet), чтобы
    сверка казны не смешивала контуры. unit_ref — тот же идемпотент, что у гранта.
    """

    __tablename__ = "incomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))
    amount_stars: Mapped[int] = mapped_column(Integer, default=0)
    amount_nanotons: Mapped[int] = mapped_column(BigInteger, default=0)
    round_id: Mapped[int | None] = mapped_column(ForeignKey("rounds.id"), nullable=True, index=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True, index=True)
    network: Mapped[str] = mapped_column(String(16), default="mainnet")
    unit_ref: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WalletDialog(Base):
    """Диалог привязки кошелька в личке: игрок → ожидаем адрес следующим сообщением.

    В таблице, а не в памяти процесса: переживает рестарт и работает при
    нескольких инстансах за одним вебхуком.
    """

    __tablename__ = "wallet_dialogs"

    player_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    since: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PreparedDay(Base):
    """Заготовка следующего дня, сгенерированная заранее в час подсчёта.

    payload — JSON: глава, правило с солью, пути к готовым картинкам.
    Материализация в раунд в момент открытия занимает миллисекунды, поэтому
    сетка 11:00 не плывёт на время нейрогенераций. Если заготовки нет
    (рестарт посреди генерации) — создание дня падает обратно в старый
    синхронный путь без потерь.
    """

    __tablename__ = "prepared_days"

    day_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MemoryHit(Base):
    """Отметка внимательности: игрок узнал тихий след давнего дня в каноне.

    Бот никогда не подтверждает и не опровергает догадку — только копит
    счётчик «Память лабиринта», видимый в /score. Одна отметка на игрока в день.
    """

    __tablename__ = "memory_hits"

    player_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    round_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BestiarySighting(Base):
    """Встреча Стаи с существом Лабиринта: запись бестиария /best.

    Уникальность (сезон, ключ) делает запись идемпотентной: сколько бы раз
    ни выпала маска дня, существо попадает в бестиарий один раз за сезон.
    """

    __tablename__ = "bestiary_sightings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season: Mapped[str] = mapped_column(String(16), nullable=False)
    beast_key: Mapped[str] = mapped_column(String(32), nullable=False)
    day_index: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("season", "beast_key", name="uq_bestiary_season_beast"),
    )


class WorldScar(Base):
    """Шрам мира — след от выбора стаи, меняющий лабиринт.

    Шрамы создаются, когда стая делает значимые выборы (сжигает мосты,
    обманывает датчики, помогает чужим). Каждый шрам может блокировать
    локации, разблокировать новые, менять тон атмосферы.
    """

    __tablename__ = "world_scars"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scar_key: Mapped[str] = mapped_column(String(64), index=True)
    created_day: Mapped[int] = mapped_column(Integer)
    expires_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    strength: Mapped[int] = mapped_column(Integer, default=1)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmotionalState(Base):
    """Эмоциональный профиль стаи — накапливается от выборов.

    Три параметра (0-10):
    - fatigue: усталость (от risk)
    - hope: надежда (от care)
    - paranoia: паранойя (от cunning)

    Влияют на тон атмосферных пэдов, доступность NPC-взаимодействий,
    варианты финала и распределение карточек.
    """

    __tablename__ = "emotional_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fatigue: Mapped[int] = mapped_column(Integer, default=0)
    hope: Mapped[int] = mapped_column(Integer, default=0)
    paranoia: Mapped[int] = mapped_column(Integer, default=0)
    last_updated_day: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
