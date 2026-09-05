"""Исходящие выплаты победителям: расчёт в stakes.finalize_day_payouts,
отправка здесь — через pytoniq напрямую к лайтсерверам активной сети.
Казначейский кошелёк поддерживается в двух версиях контракта — v4r2 и
v5r1 (кошельки нового поколения): версия детектируется автоматически по
адресу казначея либо задаётся явно переменной TREASURY_WALLET_VERSION.

Жизненный цикл выплаты: pending → sending (зафиксировано ДО вещания, чтобы
падение сервиса не привело к двойной отправке) → sent / failed. Зависшие
sending и неуспешные failed с attempts < PAYOUT_MAX_ATTEMPTS оживают каждый
цикл автоматически; при исчерпании лимита админ получает алерт. Перед
ПОВТОРНОЙ отправкой (attempts > 1) очередь сверяется с memo недавних
исходящих казначея: если перевод уже ушёл в цепочку в прошлый раз, он
помечается sent без повтора — краш между вещанием и коммитом не задваивает
платёж. Ручной retry (/payout, resolve_dead_payout) счётчик попыток НЕ
сбрасывает: он сам мог повернуть в очередь уже ушедший перевод, и только
attempts >= 1 заставляет диспетчер сверяться с историей перед отправкой.

Призы и возвраты без получателя (игрок не привязал кошелёк к моменту
финализации) не тонут в failed: строки ждут в очереди, и когда игрок
привязывает адрес, диспетчер вставляет его в следующий же цикл и платёж
уходит сам. Доли казны без OWNER_WALLET_ADDRESS и переводы без игрока
честно падают в failed с причиной-действием.

tx_hash после отправки — метка вещания «bcast:<unix>»: лайтсервер не
возвращает хеш транзакции. Фактический перевод ищется в эксплорере по адресу
казначея и memo-комментарию вида way:<день>:<тип>#<id выплаты>.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import datetime, timezone

import httpx
from aiogram import Bot
from sqlalchemy import func, select, update

from app.config import settings
from app.db import SessionLocal
from app.models import Income, Payout, Player, Round, RoundStatus, WatcherState
from app.stakes import finalize_day_payouts
from app.ton_utils import friendly_address, from_nano, normalize_address, to_nano

logger = logging.getLogger(__name__)


async def _http_get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    max_retries: int = 1,
    retry_delay: float = 1.0,
) -> httpx.Response:
    """HTTP GET с retry для 5xx ошибок."""
    last_exc = None
    for attempt in range(1 + max_retries):
        try:
            response = await client.get(url, params=params, headers=headers)
            if response.status_code < 500 or attempt == max_retries:
                return response
            logger.warning(
                "HTTP %d от %s (попытка %d/%d), повтор через %.1fs",
                response.status_code, url, attempt + 1, 1 + max_retries, retry_delay,
            )
            await asyncio.sleep(retry_delay)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt == max_retries:
                raise
            logger.warning(
                "HTTP ошибка %s от %s (попытка %d/%d), повтор через %.1fs",
                exc, url, attempt + 1, 1 + max_retries, retry_delay,
            )
            await asyncio.sleep(retry_delay)
    raise last_exc  # type: ignore[misc]

# Синглтон кошелька казначея: подключение к лайтсерверам дорогое, держим одно.
_wallet_lock = asyncio.Lock()
_provider = None
_wallet = None
_wallet_network: str | None = None

# Глобальный идентификатор сети (конфиг #19 блокчейна): входит в wallet_id
# контракта v5, поэтому с одной мнемоникой тестнет- и мейннет-v5-кошельки
# имеют разные адреса.
NETWORK_GLOBAL_IDS = {"mainnet": -239, "testnet": -3}
# Поддерживаемые версии контракта казначея.
WALLET_VERSIONS = ("v4r2", "v5r1")


def _wallet_address(version: str, public_key: bytes, network_global_id: int, wc: int = 0) -> str:
    """Адрес кошелька данной версии для ключа — чистая локальная математика.

    Адрес = хеш StateInit(code + data), сеть не нужна. Data-ячейка v4 сеть не
    задаёт (адрес одинаков в обеих сетях), у v5 network_global_id входит в
    wallet_id внутри data.
    """
    from pytoniq.contract.wallets.wallet import WALLET_V4_R2_CODE, WalletV4R2
    from pytoniq.contract.wallets.wallet_v5 import WALLET_V5_R1_CODE, WalletV5R1
    from pytoniq_core.tlb.account import StateInit

    if version == "v4r2":
        data = WalletV4R2.create_data_cell(public_key=public_key, wc=wc)
        code = WALLET_V4_R2_CODE
    elif version == "v5r1":
        data = WalletV5R1.create_data_cell(public_key=public_key, wc=wc, network_global_id=network_global_id)
        code = WALLET_V5_R1_CODE
    else:
        raise ValueError(f"Неизвестная версия кошелька казначея: {version}")
    state_init = StateInit(code=code, data=data)
    return f"{wc}:{state_init.serialize().hash.hex()}"


def _detect_wallet_version(
    public_key: bytes, treasury_address: str, network_global_id: int
) -> tuple[str | None, dict[str, str]]:
    """Версия кошелька, чей производный адрес совпал с настроенным.

    Возвращает (версия | None, {версия: адрес-кандидат}) — кандидаты идут в
    текст ошибки, чтобы расхождение мнемоники и адреса было видно сразу.
    """
    target = normalize_address(treasury_address)
    candidates = {
        version: _wallet_address(version, public_key, network_global_id)
        for version in WALLET_VERSIONS
    }
    for version, address in candidates.items():
        if normalize_address(address) == target:
            return version, candidates
    return None, candidates


async def pending_payout_count(session) -> int:
    """Сколько переводов ещё не ушли (обе сети, включая dead-letter failed).

    «sent» — единственное конечное состояние успеха; «dismissed» — ручной
    вердикт хранителя (спам-перевод с рекламой и т.п.), он деньгам игрокам
    не равен и сбросу не мешает. Всё остальное значит, что деньги игроку
    ещё должны: сброс игры обязан ждать, пока долг закрыт.
    """
    result = await session.execute(
        select(func.count()).select_from(Payout).where(Payout.status.notin_(["sent", "dismissed"]))
    )
    return int(result.scalar_one())


async def resolve_dead_payout(session, payout_id: int, action: str) -> str | None:
    """Ручной разбор проблемной выплаты хранителем.

    action="spam" — статус «dismissed»: пыльный спам-перевод с рекламой,
    возврат которого не нужен или невозможен. Выплата исчезает из очереди,
    алертов и перестаёт блокировать /resetgame. action="retry" — обратно в
    очередь (настоящий долг игроку). Счётчик попыток НЕ сбрасываем: попытка
    могла реально уйти в цепочку (краш между вещанием и коммитом «sent»), и
    повтор без сверки с memo казначея задвоил бы платёж. Значение attempts
    >= 1 гарантирует, что диспетчер прогонит анти-дубль по истории исходящих.
    Возвращает новый статус или None, если выплаты нет либо она уже отправлена.
    """
    payout = await session.get(Payout, payout_id)
    if payout is None or payout.status == "sent":
        return None
    if action == "spam":
        payout.status = "dismissed"
    elif action == "retry":
        payout.status = "pending"
        payout.alerted = False
    else:
        return None
    await session.commit()
    logger.info("Выплата %d разобрана вручную: %s", payout_id, payout.status)
    return payout.status


async def _fetch_remote_json(url: str) -> dict:
    """Скачивает JSON (конфиг лайтсерверов) с редиректами."""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


async def _get_wallet():
    """Ленивая инициализация кошелька казначея для активной сети.

    Версия контракта — из TREASURY_WALLET_VERSION («auto» = детект по адресу).
    Проверка пары мнемоника/адрес выполняется ДО подключения к сети: если
    производный адрес не совпал, отправлять нельзя в принципе — падаем с
    внятной ошибкой, а не молчаливыми неудачными выплатами. Источник
    лайтсерверов: LITESERVER_CONFIG_URL (свежий JSON), иначе встроенный
    конфиг pytoniq для сети.
    """
    global _provider, _wallet, _wallet_network
    network = "testnet" if settings.is_testnet else "mainnet"
    async with _wallet_lock:
        if _wallet is not None and _wallet_network == network:
            return _wallet
        if not settings.active_treasury_mnemonic:
            raise ValueError("Нет мнемоники казначея для активной сети")
        if not settings.active_treasury_address:
            raise ValueError("Нет адреса казначея для активной сети")
        words = settings.active_treasury_mnemonic.replace("\n", " ").split()
        if len(words) < 12:
            raise ValueError("Мнемоника казначея неполная (нужно 24 слова)")

        from pytoniq import LiteBalancer
        from pytoniq.contract.wallets.wallet import WalletV4R2
        from pytoniq.contract.wallets.wallet_v5 import WalletV5R1
        from pytoniq_core.crypto.keys import mnemonic_to_private_key, private_key_to_public_key

        _, private_key = mnemonic_to_private_key(words)
        public_key = private_key_to_public_key(private_key)
        network_global_id = NETWORK_GLOBAL_IDS[network]

        requested = settings.treasury_wallet_version.strip().lower()
        if requested in WALLET_VERSIONS:
            derived = _wallet_address(requested, public_key, network_global_id)
            if normalize_address(derived) != normalize_address(settings.active_treasury_address):
                raise ValueError(
                    f"Адрес казначея не совпадает с производным от мнемоники "
                    f"(TREASURY_WALLET_VERSION={requested}): {derived}. "
                    "Проверь пару мнемоника/адрес или верни auto."
                )
            version = requested
        else:
            version, candidates = _detect_wallet_version(
                public_key, settings.active_treasury_address, network_global_id
            )
            if version is None:
                raise ValueError(
                    "Адрес казначея не совпадает ни с одной поддерживаемой версией "
                    f"кошелька для этой мнемоники: {candidates}. Проверь адрес и "
                    "мнемонику, либо задай TREASURY_WALLET_VERSION=v4r2|v5r1 явно."
                )

        if _provider is not None:
            try:
                await _provider.close_all()
            except Exception:
                pass
            _provider = None
            _wallet = None
        if settings.liteserver_config_url:
            config = await _fetch_remote_json(settings.liteserver_config_url)
            _provider = LiteBalancer.from_config(config)
            logger.info("Лайтсерверы: конфиг из LITESERVER_CONFIG_URL")
        elif network == "testnet":
            _provider = LiteBalancer.from_testnet_config()
        else:
            _provider = LiteBalancer.from_mainnet_config()
        await _provider.start_up()
        if version == "v5r1":
            _wallet = await WalletV5R1.from_private_key(
                _provider, private_key=private_key, wc=0, network_global_id=network_global_id
            )
        else:
            _wallet = await WalletV4R2.from_private_key(_provider, private_key, wc=0)
        _wallet_network = network
        logger.info("Кошелёк казначея готов (%s, контракт %s)", network, version)
        return _wallet


def _comment_cell(text: str):
    from pytoniq_core import begin_cell

    return begin_cell().store_uint(0, 32).store_string(text[:120]).end_cell()


# ---------- Анти-дубль: сверка memo с историей казначея ----------


def _out_comments_tonapi(item: dict) -> list[str]:
    """Комментарии исходящих сообщений одной транзакции (формат TonAPI v2)."""
    comments: list[str] = []
    for msg in item.get("out_msgs") or []:
        if not isinstance(msg, dict):
            continue
        text = str(msg.get("raw_message") or "")
        if not text:
            msg_data = msg.get("msg_data") or {}
            if isinstance(msg_data, dict):
                text = str(msg_data.get("decoded_comment") or "")
                if not text:
                    b64 = msg_data.get("text")
                    if b64:
                        try:
                            text = base64.b64decode(str(b64)).decode("utf-8", "ignore")
                        except Exception:
                            text = ""
        if text:
            comments.append(text)
    return comments


def _out_comments_toncenter(item: dict) -> list[str]:
    """Комментарии исходящих сообщений одной транзакции (формат Toncenter v3)."""
    comments: list[str] = []
    for msg in item.get("out_msgs") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("message_content") or {}
        decoded = content.get("decoded") if isinstance(content, dict) else None
        if isinstance(decoded, dict) and decoded.get("@type") == "comment":
            text = str(decoded.get("comment") or "")
            if text:
                comments.append(text)
    return comments


async def _markers_via_tonapi() -> set[str]:
    url = f"{settings.active_ton_api_base}/v2/accounts/{settings.active_treasury_address}/transactions"
    headers = {"X-API-Key": settings.ton_api_key} if settings.ton_api_key else {}
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url, params={"limit": 128, "sort_order": "desc"}, headers=headers)
        response.raise_for_status()
        items = response.json().get("transactions") or []
    markers: set[str] = set()
    for item in items:
        markers.update(_out_comments_tonapi(item))
    return markers


async def _markers_via_toncenter() -> set[str]:
    url = f"{settings.active_toncenter_api_base.rstrip('/')}/api/v3/transactions"
    params = {
        "account": settings.active_treasury_address,
        "limit": 128,
        "sort": "desc",
    }
    headers = {"X-API-Key": settings.toncenter_api_key} if settings.toncenter_api_key else {}
    async with httpx.AsyncClient(timeout=15) as client:
        response = await _http_get_with_retry(client, url, params=params, headers=headers)
        response.raise_for_status()
        items = response.json().get("transactions") or []
    markers: set[str] = set()
    for item in items:
        markers.update(_out_comments_toncenter(item))
    return markers


async def fetch_broadcast_markers() -> set[str]:
    """Memo недавних исходящих переводов казначея (TonAPI → фолбэк Toncenter).

    Сверка перед ПОВТОРНОЙ отправкой: перевод мог уйти в цепочку в прошлый
    раз, но статус «sent» сохранить не успели (краш/таймаут сразу после
    вещания). Повтор такой выплаты — реальные чужие деньги дважды. Пустой
    результат при сбое сети значит «не знаем»: ведём себя как раньше и
    пытаемся отправить — узкое окно риска лучше постоянной блокировки очереди.
    """
    for fetch in (_markers_via_tonapi, _markers_via_toncenter):
        try:
            return await fetch()
        except Exception as exc:
            logger.warning("История исходящих казначея (%s) недоступна: %s", fetch.__name__, exc)
    return set()


async def send_ton_transfer(dest_address: str, amount_nanotons: int, comment: str) -> str | None:
    """Отправляет перевод с казначея. Возвращает метку вещания или None.

    None — только когда отправка невозможна в принципе (TON выключен или нет
    мнемоники): вызывающий диспетчер сам запишет понятную причину в
    payouts.last_error. Реальные ошибки (пара мнемоника/адрес, лайтсерверы,
    seqno) ПРОПАГАЦИЯТСЯ исключением — диспетчер кладёт их текст в
    last_error, и причина видна в /payouts и алертах без раскопок логов.
    Успех фиксируется лайтсервером (результат 1); фактический хеш транзакции
    смотрится в эксплорере по memo-комментарию.
    """
    if not settings.ton_enabled or not settings.active_treasury_mnemonic:
        logger.warning("TON выключен или нет мнемоники: выплата к …%s не отправлена", dest_address[-6:])
        return None
    wallet = await _get_wallet()
    result = await wallet.transfer(
        destination=dest_address,
        amount=amount_nanotons,
        body=_comment_cell(comment),
    )
    if result != 1:
        raise RuntimeError(f"Лайтсерверы не приняли перевод (результат {result})")
    marker = f"bcast:{int(datetime.now(timezone.utc).timestamp())}"
    logger.info("Перевод %d нанотонов к …%s разослан (%s)", amount_nanotons, dest_address[-6:], comment[:40])
    return marker


async def _reset_retriable(session, network: str) -> None:
    """Оживляем зависшие sending/failed, пока не исчерпан лимит попыток."""
    await session.execute(
        update(Payout)
        .where(
            Payout.status.in_(["failed", "sending"]),
            Payout.attempts < settings.payout_max_attempts,
            Payout.dest_address != "",
            Payout.network == network,
        )
        .values(status="pending")
    )


async def _alert_admin(bot: Bot | None, network: str) -> None:
    """Алерты о failed-выплатах. Дедуп — колонка payouts.alerted в БД:
    переживает рестарт и безопасен при нескольких инстансах. В текст идут
    причины из last_error — разбор начинается без открытия логов."""
    if bot is None:
        return
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Payout.id, Payout.last_error).where(
                        Payout.status == "failed",
                        Payout.alerted.is_(False),
                        Payout.network == network,
                    )
                )
            )
            .all()
        )
        if not rows:
            return
        sample = "; ".join(
            f"#{payout_id}: {reason}" if reason else f"#{payout_id}"
            for payout_id, reason in rows[:3]
        )
        text = (
            f"⚠️ Выплаты не ушли ({len(rows)} шт., сеть {network}). {sample}. "
            "Разбор: /payouts (причина видна у каждой строки)."
        )
        await session.execute(
            update(Payout)
            .where(Payout.id.in_([row_id for row_id, _reason in rows]))
            .values(alerted=True)
        )
        await session.commit()
    for admin_id in settings.admin_id_set:
        try:
            await bot.send_message(admin_id, text)
        except Exception as exc:
            logger.warning("Алерт админу %s не доставлен: %s", admin_id, exc)


# Доли казны без игрока: адрес получателя — OWNER_WALLET_ADDRESS.
_TREASURY_KINDS = {"rake", "leaderboard"}


async def _hydrate_player_dests(session, network: str) -> int:
    """Оживляет выплаты без получателя, когда кошелёк уже привязан.

    Призы и возвраты игроков без привязанного кошелька на момент финализации
    не должны тонуть в failed (деньги спят, пока админ не разберёт вручную).
    Строка остаётся в очереди, а как только игрок привязывает адрес (/wallet),
    следующий же цикл диспетчера всталяет его в dest_address и платёж уходит
    сам — retry из /payouts не нужен. Доли казны (rake/leaderboard) без
    OWNER_WALLET_ADDRESS и выплаты без игрока (player_id пуст) оживлять нечем:
    честный failed с причиной-действием, как раньше.

    Возвращает число оживших строк (они поедут в пик этого же цикла).
    """
    rows = list(
        (
            await session.execute(
                select(Payout).where(
                    Payout.dest_address == "",
                    Payout.status == "pending",
                    Payout.network == network,
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0
    player_ids = {p.player_id for p in rows if p.player_id is not None}
    wallet_map: dict[int, str] = {}
    if player_ids:
        players = await session.execute(
            select(Player.id, Player.wallet_address).where(Player.id.in_(player_ids))
        )
        wallet_map = {pid: addr for pid, addr in players.all()}
    revived = 0
    for payout in rows:
        if payout.kind in _TREASURY_KINDS:
            if settings.owner_wallet_address:
                payout.dest_address = normalize_address(settings.owner_wallet_address)
                payout.last_error = None
                revived += 1
            else:
                payout.status = "failed"
                payout.last_error = "нет адреса получателя: для доли казны задай OWNER_WALLET_ADDRESS"
        elif payout.player_id is None:
            payout.status = "failed"
            payout.last_error = "нет адреса получателя (кошелёк игрока не найден)"
        else:
            addr = wallet_map.get(payout.player_id) or ""
            if addr:
                payout.dest_address = addr
                payout.last_error = None
                revived += 1
            else:
                payout.last_error = "нет адреса получателя: кошелёк игрока ещё не привязан"
    await session.commit()
    return revived


async def dispatch_pending_payouts(limit: int = 50, bot: Bot | None = None) -> int:
    sent = 0
    network = "testnet" if settings.is_testnet else "mainnet"
    async with SessionLocal() as session:
        # Ретрай: зависшие failed с неисчерпанным лимитом снова в очередь.
        await _reset_retriable(session, network)
        await session.commit()
        # Призы без кошелька оживают сами, когда игрок привязал адрес: это
        # отдельный проход, а НЕ статус failed, иначе строки тонули бы в
        # мёртвых письмах, а игрок терял бы деньги без веской причины.
        await _hydrate_player_dests(session, network)
        result = await session.execute(
            select(Payout)
            .where(
                Payout.status == "pending",
                # Пустые получатели в пик не берём: они либо оживают выше в этом
                # же цикле, либо ждут кошелёк. Иначе они съедали бы лимит из
                # 50 строк и голодали настоящие выплаты.
                Payout.dest_address != "",
                Payout.amount_nanotons > 0,
                Payout.network == network,
            )
            .order_by(Payout.id.asc())
            .limit(limit)
        )
        payouts = list(result.scalars().all())
        # Предохранитель баланса: не вещаем переводы, которые сеть отвергнет
        # из-за нехватки средств на казначее. Fail-fast с понятной причиной:
        # статус остаётся pending, попытки НЕ сгорают — после пополнения
        # очередь уйдёт сама, без ручного retry и без мёртвых писем.
        #
        # Если баланс недоступен (оба индексатора молчат) — логируем, но
        # ПРОБУЕМ отправить: liteclient работает через прямое TCP-соединение
        # к liteserver, а не через HTTP API. Пусть liteserver отвергнет сам,
        # если средств мало — это надёжнее, чем висеть в очереди навсегда.
        sendable = [payout for payout in payouts if payout.dest_address]
        if (
            sendable
            and settings.active_treasury_address
            and settings.active_treasury_mnemonic
        ):
            try:
                balance, _status, _source = await fetch_account_state()
            except Exception as exc:
                logger.warning("Баланс казначея перед циклом не прочитан: %s", exc)
                balance = None
            if balance is None:
                logger.warning(
                    "Баланс казначея недоступен (оба индексатора молчат) — "
                    "попытка отправки через liteclient напрямую (%d выплат)",
                    len(sendable),
                )
            if balance is not None:
                fee_nano = to_nano(settings.payout_fee_gram)
                needed = sum(p.amount_nanotons for p in sendable) + fee_nano * len(sendable)
                if balance < needed:
                    reason = (
                        f"казначей подкачан: нужно {needed / 1e9:.4f} Gram (с газом), "
                        f"есть {balance / 1e9:.4f} — пополни баланс, очередь уйдёт сама"
                    )
                    logger.warning("Диспетчер: %s", reason)
                    for payout in sendable:
                        payout.last_error = reason[:200]
                    await session.commit()
                    return 0
        # Атомарный клейм «взятых в работу» ДО вещания. Условный UPDATE по
        # status='pending' — единственный процесс (одна копия диспетчера)
        # переведёт строку в sending: rowcount==1. Вторая копия (двойной
        # процесс: закрытие дня + ton-settle + ручной кик) получит 0 и НЕ
        # возьмёт строку — иначе оба вещали бы один перевод и задваивали
        # трату казны. Падение после клейма обратимо: _reset_retriable вернёт
        # sending → pending на следующем цикле, а memo-антидубль (attempts>1)
        # уберёт повтор уже ушедшего перевода.
        claimed_ids: set[int] = set()
        for payout in payouts:
            if not payout.dest_address:
                continue
            gate = await session.execute(
                update(Payout)
                .where(Payout.id == payout.id, Payout.status == "pending")
                .values(status="sending")
            )
            if gate.rowcount != 1:
                # Строку уже забрала другая копия — не трогаем и не вещаем.
                continue
            payout.attempts += 1
            payout.status = "sending"
            claimed_ids.add(payout.id)
        await session.commit()
        # Работаем только строками, что реально забрали мы: сама рассылка
        # (claimed). Строки другой копии диспетчера в эту сессию НЕ трогаем.
        payouts = [p for p in payouts if p.id in claimed_ids]
        # Сверка с историей: если комментарий уже есть среди недавних
        # исходящих казначея — перевод ушёл в прошлом цикле (краш между
        # вещанием и коммитом). Повторная отправка задвоила бы платёж.
        markers: set[str] = set()
        if payouts:
            markers = await fetch_broadcast_markers()
        for payout in payouts:
            # Свободный комментарий (возвраты при паузе) либо служебное memo
            # «way:<день>:<тип>#<id>» — по нему же работает анти-дубль.
            comment = payout.comment_override or f"way:{payout.round_id}:{payout.kind}#{payout.id}"
            if comment in markers:
                # Перевод уже ушёл в цепочку раньше, но статус тогда не
                # сохранился (краш/таймаут после вещания). Повтор задвоил бы
                # платёж — фиксируем доставку без новой отправки.
                payout.tx_hash = None
                payout.status = "sent"
                payout.sent_at = datetime.now(timezone.utc)
                payout.last_error = None
                sent += 1
                logger.warning(
                    "Выплата %d уже разослана ранее (memo найдено у казначея) — помечена sent без повтора",
                    payout.id,
                )
                continue
            try:
                tx_hash = await asyncio.wait_for(
                    send_ton_transfer(
                        payout.dest_address,
                        payout.amount_nanotons,
                        comment=comment,
                    ),
                    timeout=settings.payout_send_timeout_seconds,
                )
            except asyncio.TimeoutError:
                # Зависший лайтсервер не имеет права замораживать цикл:
                # таймаут — обычный ретрай с видимой причиной.
                logger.warning("Выплата %s: таймаут вещания >%ss", payout.id, settings.payout_send_timeout_seconds)
                payout.last_error = f"таймаут вещания (>{settings.payout_send_timeout_seconds} с)"
                tx_hash = None
            except Exception as exc:
                reason = str(exc)
                if "no alive peers" in reason.lower():
                    # Типовой тестнет-случай: встроенный конфиг pytoniq мёртв
                    # или UDP закрыт окружением. Причина должна звать к решению.
                    logger.warning("Выплата %s: нет живых лайтсерверов", payout.id)
                    reason = (
                        "have no alive peers: лайтсерверы недоступны — задай "
                        "LITESERVER_CONFIG_URL с живым конфигом тестнета "
                        "(https://ton.org/testnet-global.config.json) или разошли "
                        "очередь локально на той же БД"
                    )
                else:
                    logger.warning("Выплата %s не ушла: %s", payout.id, exc)
                payout.last_error = reason[:200]
                tx_hash = None
            if tx_hash is None and payout.last_error is None:
                # Единственный путь сюда — guard выключенного TON/мнемоники.
                payout.last_error = "отправка недоступна: TON выключен или нет мнемоники казначея"
            if tx_hash:
                payout.tx_hash = tx_hash
                payout.status = "sent"
                payout.sent_at = datetime.now(timezone.utc)
                payout.attempts = 0
                payout.last_error = None
                sent += 1
            elif payout.attempts >= settings.payout_max_attempts:
                payout.status = "failed"
            else:
                # Лимит не исчерпан — вернётся в очередь следующего цикла;
                # last_error сохраняем: причина видна в /payouts уже сейчас.
                payout.status = "pending"
        await session.commit()
    dead = [p.id for p in payouts if p.status == "failed"]
    if dead:
        logger.warning("Выплаты окончательно не отправлены: %s", dead)
    # Алерт по ВСЕМ неотправленным без предупреждения (включая найденные
    # после рестарта): дедуп внутри _alert_admin по колонке alerted.
    await _alert_admin(bot, network)
    return sent


async def settle_closed_rounds(bot: Bot | None = None) -> int:
    """Финализирует фонды закрытых дней и разбирает очередь выплат."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(Round.id).where(
                Round.status == RoundStatus.CLOSED,
                Round.payouts_finalized.is_(False),
            )
        )
        round_ids = [row[0] for row in result.all()]
    created = 0
    for round_id in round_ids:
        async with SessionLocal() as session:
            round_row = await session.get(Round, round_id)
            if round_row is not None:
                created += await finalize_day_payouts(session, round_row)
    await dispatch_pending_payouts(bot=bot)
    return created


# ---------- Диагностика казначея для хранителя (/treasury) ----------


async def _tonapi_account_raw(address: str) -> dict:
    url = f"{settings.active_ton_api_base}/v2/accounts/{address}"
    headers = {"X-API-Key": settings.ton_api_key} if settings.ton_api_key else {}
    async with httpx.AsyncClient(timeout=15) as client:
        response = await _http_get_with_retry(client, url, headers=headers)
        response.raise_for_status()
        return response.json()


async def _toncenter_account(address: str) -> dict:
    url = f"{settings.active_toncenter_api_base.rstrip('/')}/api/v3/accountInformation"
    headers = {"X-API-Key": settings.toncenter_api_key} if settings.toncenter_api_key else {}
    async with httpx.AsyncClient(timeout=15) as client:
        response = await _http_get_with_retry(client, url, params={"address": address}, headers=headers)
        response.raise_for_status()
        return response.json()


async def fetch_account_state() -> tuple[int | None, str | None, str]:
    """(баланс в нанотонах | None, статус аккаунта | None, источник данных).

    TonAPI → фолбэк Toncenter v3; оба молчат — (None, None, "none").
    """
    address = settings.active_treasury_address
    try:
        data = await _tonapi_account_raw(address)
        balance = int(str(data.get("balance") or 0))
        status = str(data.get("status") or "")
        return balance, (status or None), "tonapi"
    except Exception as exc:
        logger.warning("Баланс казначея через TonAPI недоступен: %s", exc)
    try:
        data = await _toncenter_account(address)
        return int(str(data.get("balance") or 0)), None, "toncenter"
    except Exception as exc:
        logger.warning("Баланс казначея через Toncenter недоступен: %s", exc)
    return None, None, "none"


def treasury_pair_check_text() -> str:
    """Сверка пары мнемоника/адрес без выхода в сеть: v4r2/v5r1 → адрес."""
    from pytoniq_core.crypto.keys import mnemonic_to_private_key, private_key_to_public_key

    words = settings.active_treasury_mnemonic.replace("\n", " ").split()
    if len(words) < 12:
        return f"мнемоника неполная ({len(words)} слов вместо 24) ⚠️"
    try:
        _, private_key = mnemonic_to_private_key(words)
    except Exception:
        return "мнемоника невалидна ⚠️"
    public_key = private_key_to_public_key(private_key)
    network = "testnet" if settings.is_testnet else "mainnet"
    version, _candidates = _detect_wallet_version(
        public_key, settings.active_treasury_address, NETWORK_GLOBAL_IDS[network]
    )
    if version is not None:
        return f"{version} ✓ (детект по адресу)"
    return (
        "ни v4r2, ни v5r1 не дают настроенный адрес ⚠️ — "
        "проверь пару мнемоника/адрес или задай TREASURY_WALLET_VERSION явно"
    )


async def treasury_diagnostics() -> str:
    """Полный отчёт по казначею одной строкой-текстом для /treasury."""
    network = "testnet" if settings.is_testnet else "mainnet"
    lines = [f"🏛 Казначей ({network})"]
    if not settings.ton_enabled:
        lines.append("TON выключен (TON_ENABLED=false): ставки и выплаты не работают.")
        return "\n".join(lines)
    if settings.active_treasury_address:
        shown = friendly_address(settings.active_treasury_address, testnet=settings.is_testnet)
        lines.append(f"Адрес: <code>{shown}</code>")
    else:
        lines.append("Адрес не задан ⚠️")
    lines.append("Мнемоника: " + ("задана ✓" if settings.active_treasury_mnemonic else "НЕ задана ⚠️"))
    lines.append(
        "OWNER_WALLET_ADDRESS: "
        + ("задан ✓" if settings.owner_wallet_address else "не задан — доли казны (рейк/копилка) уйти не могут ⚠️")
    )
    if settings.active_treasury_address and settings.active_treasury_mnemonic:
        try:
            lines.append(f"Пара мнемоника/адрес: {treasury_pair_check_text()}")
        except Exception as exc:
            lines.append(f"Пара мнемоника/адрес: не проверена ({exc})")
        balance, status, source = await fetch_account_state()
        if balance is None:
            lines.append("Баланс: недоступен (оба индексатора молчат) ⚠️")
        else:
            note = f", статус {status}" if status else ""
            lines.append(f"Баланс: {from_nano(balance):.4f} Gram{note} · источник {source}")
            if balance <= 0:
                lines.append("Баланс пуст: пополнить через @testgiver_ton_bot (testnet).")
    # Сверка с ожиданиями БД, корректировки казны и стоп-кран (/adjust, /pause).
    from app.ops import (
        MANUAL_IN_KIND,
        MANUAL_OUT_KIND,
        is_game_paused,
        paused_reason,
        treasury_expected_state,
    )

    try:
        async with SessionLocal() as session:
            drift_state = await treasury_expected_state(session)
            adjustments = (
                await session.execute(
                    select(
                        Income.kind,
                        func.count(),
                        func.coalesce(func.sum(Income.amount_nanotons), 0),
                    )
                    .where(Income.kind.in_([MANUAL_OUT_KIND, MANUAL_IN_KIND]))
                    .group_by(Income.kind)
                )
            ).all()
            paused = await is_game_paused(session)
            reason = await paused_reason(session)
    except Exception:
        logger.warning("Сверка казны для /treasury не собралась", exc_info=True)
        drift_state, adjustments, paused, reason = None, [], False, None
    if paused:
        lines.append(
            f"⏸ Игра на паузе ({reason or 'техработы'}): входящие переводы "
            "возвращаются отправителям. Снять: /resume"
        )
    if adjustments:
        parts = [
            f"{'−' if kind == MANUAL_OUT_KIND else '+'}{from_nano(total):.4f} Gram ({count})"
            for kind, count, total in adjustments
        ]
        lines.append("Корректировки казны: " + " · ".join(parts))
    if drift_state is not None:
        if drift_state.beyond_tolerance:
            lines.append(
                f"Ожидания БД: ~{drift_state.expected_nanotons / 1e9:.4f} Gram · "
                f"расхождение {drift_state.drift_nanotons / 1e9:+.4f} Gram ⚠️ — "
                "закрой: /adjust"
            )
        else:
            lines.append(
                f"Сверка с БД сходится ✓ (ожидается ~{drift_state.expected_nanotons / 1e9:.4f} Gram)"
            )
    async with SessionLocal() as session:
        waiting = (
            await session.execute(
                select(func.count()).select_from(Payout).where(Payout.status.notin_(["sent", "dismissed"]))
            )
        ).scalar_one()
        dead = (
            await session.execute(select(func.count()).select_from(Payout).where(Payout.status == "failed"))
        ).scalar_one()
        # Глазами watcher'а: куда смотрит, когда последний раз видел цепочку
        # и где стоит курсор. Одна команда отвечает на «почему не видно пополнений».
        from app.ton_watch import BEAT_KEY, CURSOR_KEY, SOURCE_KEY

        beat_iso = None
        source = None
        cursor_raw = None
        for key, slot in ((BEAT_KEY, "b"), (SOURCE_KEY, "s"), (CURSOR_KEY, "c")):
            row = await session.get(WatcherState, key)
            if row is not None:
                if slot == "b":
                    beat_iso = row.value
                elif slot == "s":
                    source = row.value
                else:
                    cursor_raw = row.value
    lines.append(f"Очередь выплат: ожидает {waiting} · failed {dead}")
    if waiting or dead:
        lines.append("Разбор: /payouts — причина видна у каждой строки.")
    now = datetime.now(timezone.utc)
    lines.append("Watcher:")
    if not settings.active_treasury_address:
        lines.append("  адрес не задан — смотреть не на что ⚠️")
    else:
        lines.append(f"  смотрит на: {settings.active_treasury_address[:8]}…{settings.active_treasury_address[-6:]} ({network})")
    beat_age = None
    if beat_iso:
        try:
            beat_moment = datetime.fromisoformat(beat_iso)
            if beat_moment.tzinfo is None:
                beat_moment = beat_moment.replace(tzinfo=timezone.utc)
            beat_age = int((now - beat_moment).total_seconds())
        except ValueError:
            pass
    lines.append(
        f"  успешный цикл: {'never' if beat_age is None else f'{beat_age} с назад'}"
        + (f" · источник {source}" if source else "")
    )
    if beat_age is not None and beat_age > 180:
        lines.append("  ⚠️ циклы не проходят >3 мин: индексаторы недоступны или процесс спит")
    if cursor_raw and cursor_raw.isdigit():
        cursor_dt = datetime.fromtimestamp(int(cursor_raw), tz=timezone.utc)
        lag = int((now - cursor_dt).total_seconds())
        lines.append(f"  курсор: {cursor_dt:%d.%m %H:%M} UTC ({lag:+d} с от текущего времени)")
        if lag < -60:
            lines.append("  ⚠️ курсор В БУДУЩЕМ: новые переводы отсекаются как «старые» — обнули ключ ton_watch_cursor_utime в watcher_state")
    elif settings.ton_enabled:
        lines.append("  курсора нет — стартует с отката 12 ч")
    return "\n".join(lines)
