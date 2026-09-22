"""Зеркало казны: независимая копия истории активного кошелька казначея.

Сверка «тютелька в тютельку» достигается построением: зеркало хранит каждую
цепочечную транзакцию (сторону казначея) со своим balance_delta, а баланс
кошелька = Σ balance_delta от генезиса до головы цепочки. Отсюда:

  * нет допуска на газ — реальный fee берётся из цепочки (total_fees), а не
    из оценки payout_fee_gram, поэтому накопленный сдвиг оценки с ростом N
    исходящих НЕ превращается в «расхождение»;
  * неопознанные входящие (пыль, переводы мимо бота) видны строками
    kind=unknown_in — их сумма аудируется отдельно, а не мажется по общему
    допуску;
  * после бутстрапа (зеркало покрыло генезис→голову) тождество проверяется
    на каждом цикле синка и падает ровно на разницу Σ vs живой баланс:
    ноль в отчёте означает «сходится ±0» без всяких оговорок.

Модуль цепляется в:
  - ton_pay.treasury_diagnostics() — блок «Зеркало казны» в /treasury;
  - планировщик — фоновый синк (treasury_mirror_interval_seconds);
  - ops.check_anomalies() — ежедневная автосверка (один раз в сутки).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.payments import parse_revote_memo, parse_verify_memo
from app.ton_codec import extract_comment, norm_tx_hash
from app.ton_utils import normalize_address

logger = logging.getLogger(__name__)

# Страница истории (лимит индексатора), совмещена с выбором watcher'а.
_MIRROR_PAGE_LIMIT = 100


@dataclass(frozen=True)
class MirrorMove:
    """Нормализованная транзакция казначея со стороны кошелька."""

    tx_hash: str
    network: str
    utime: int
    lt: int
    direction: str  # in | out | self | other
    value_nanotons: int
    fee_nanotons: int
    balance_delta_nanotons: int
    counterparty: str
    comment: str
    success: bool
    provider: str = ""  # tonapi | toncenter

    @property
    def is_money_move(self) -> bool:
        return self.balance_delta_nanotons != 0


def _address_of(node: Any) -> str:
    """Адрес из конверта провайдера: строка или {'address': ...} (TonAPI)."""
    if isinstance(node, dict):
        return str(node.get("address") or "")
    return str(node or "")


def _self_direction(counterparty: str, treasury: str) -> bool:
    """Самоперевод казначея самому себе: сторона совпадает с активным адресом."""
    if not treasury or not counterparty:
        return False
    try:
        return normalize_address(counterparty) == normalize_address(treasury)
    except Exception:
        return False


def _derive_balance_delta(provider_delta: Any, in_value: int, out_value: int, fee: int) -> int:
    """Сальдо аккаунта: из цепочки, если провайдер дал; иначе вычислить."""
    if provider_delta is not None:
        try:
            return int(str(provider_delta))
        except (TypeError, ValueError):
            pass
    return in_value - out_value - fee


def _in_value(item: dict) -> tuple[int, str, str]:
    """(сумма, отправитель, комментарий) входящего сообщения транзакции."""
    in_msg = item.get("in_msg") or {}
    if not isinstance(in_msg, dict):
        return 0, "", ""
    try:
        value = int(in_msg.get("value") or 0)
    except (TypeError, ValueError):
        value = 0
    source = _address_of(in_msg.get("source"))
    comment = extract_comment(in_msg)
    return value, source, comment


def _out_value(item: dict) -> tuple[int, str, str]:
    """(сумма, получатель, комментарий) исходящих сообщений транзакции."""
    out_msgs = item.get("out_msgs") or []
    total = 0
    dest = ""
    comment = ""
    for msg in out_msgs:
        if not isinstance(msg, dict):
            continue
        try:
            value = int(msg.get("value") or 0)
        except (TypeError, ValueError):
            value = 0
        total += value
        if value > 0 and not dest:
            dest = _address_of(msg.get("destination"))
            comment = extract_comment(msg)
    return total, dest, comment


def parse_tonapi_move(item: dict, network: str, treasury: str = "") -> MirrorMove | None:
    """Транзакция TonAPI v2 -> нормализованное движение зеркала.

    Pure-функция над примитивом индексатора (никакой сети и БД): тестируется
    на фикстурах. Схлапывает входящее/исходящее сообщения и реальную комиссию
    (total_fees) в одно движение со знаком balance_delta.
    """
    if not isinstance(item, dict):
        return None
    hash_raw = str(item.get("hash") or "")
    if not hash_raw:
        return None
    in_value, in_source, in_comment = _in_value(item)
    out_value, out_dest, out_comment = _out_value(item)
    try:
        utime = int(item.get("utime") or 0)
        lt = int(item.get("lt") or 0)
    except (TypeError, ValueError):
        utime = 0
        lt = 0
    try:
        fee = int(item.get("total_fees") or 0)
    except (TypeError, ValueError):
        fee = 0
    delta = _derive_balance_delta(item.get("balance_delta"), in_value, out_value, fee)
    if delta == 0 and in_value == 0 and out_value == 0:
        return None
    if in_value > 0:
        direction, counterparty, value, comment = "in", in_source, in_value, in_comment
    elif out_value > 0:
        direction, counterparty, value, comment = "out", out_dest, out_value, out_comment
    else:
        direction, counterparty, value, comment = "other", "", 0, ""
    if _self_direction(counterparty, treasury):
        direction = "self"
    success = bool(item.get("success", True))
    return MirrorMove(
        tx_hash=norm_tx_hash(hash_raw),
        network=network,
        utime=utime,
        lt=lt,
        direction=direction,
        value_nanotons=value,
        fee_nanotons=fee,
        balance_delta_nanotons=delta,
        counterparty=counterparty,
        comment=comment,
        success=success,
        provider="tonapi",
    )


def parse_toncenter_move(item: dict, network: str, treasury: str = "") -> MirrorMove | None:
    """Транзакция Toncenter v3 -> нормализованное движение зеркала.

    У Toncenter нет total_fees/balance_delta, как у TonAPI: комиссия лежит в
    поле fee, сальдо вычисляется самостоятельно (в − скидка исходящих входящих
    по этой транзакции). Это честный фолбэк для бутстрапа при молчащем TonAPI.
    """
    if not isinstance(item, dict):
        return None
    hash_raw = str(item.get("hash") or "")
    if not hash_raw:
        return None
    in_value, in_source, in_comment = _in_value(item)
    out_value, out_dest, out_comment = _out_value(item)
    try:
        utime = int(item.get("now") or 0)
        lt = int(item.get("lt") or 0)
    except (TypeError, ValueError):
        utime = 0
        lt = 0
    try:
        fee = int(item.get("fee") or 0)
    except (TypeError, ValueError):
        fee = 0
    delta = _derive_balance_delta(item.get("balance_delta"), in_value, out_value, fee)
    if delta == 0 and in_value == 0 and out_value == 0:
        return None
    if in_value > 0:
        direction, counterparty, value, comment = "in", in_source, in_value, in_comment
    elif out_value > 0:
        direction, counterparty, value, comment = "out", out_dest, out_value, out_comment
    else:
        direction, counterparty, value, comment = "other", "", 0, ""
    if _self_direction(counterparty, treasury):
        direction = "self"
    return MirrorMove(
        tx_hash=norm_tx_hash(hash_raw),
        network=network,
        utime=utime,
        lt=lt,
        direction=direction,
        value_nanotons=value,
        fee_nanotons=fee,
        balance_delta_nanotons=delta,
        counterparty=counterparty,
        comment=comment,
        success=True,
        provider="toncenter",
    )


def parse_mirror_item(item: dict, network: str, treasury: str = "") -> MirrorMove | None:
    """Движение из транзакции любого провайдера (автодетект по полям)."""
    if not isinstance(item, dict):
        return None
    if "now" in item or "fee" in item:
        return parse_toncenter_move(item, network, treasury)
    return parse_tonapi_move(item, network, treasury)


# ---------- Классификация по мемо (чистые предикаты) ----------


def parse_way_memo(comment: str) -> tuple[str, int] | None:
    """(kind, id Payout) из служебного мемо исходящего «way:<день>:<kind>#<id>».

    Мемо — глобально уникальный ключ выплаты (анти-дубль диспетчера). Возвраты
    при паузе несут тот же ключ суффиксом после свободного текста (rfind),
    поэтому легаси-строки без ключа остаются None и связываются по tx_hash.
    """
    text = (comment or "").replace("\u200b", "")
    idx = text.rfind("way:")
    if idx == -1:
        return None
    payload = text[idx + len("way:") :]
    marker = payload.rsplit("#", 1)
    if len(marker) != 2 or not marker[1].isdigit():
        return None
    kind_part = marker[0].split(":", 1)
    if len(kind_part) < 2 or not kind_part[0] or not kind_part[1]:
        return None
    return kind_part[1], int(marker[1])


def classify_incoming(comment: str) -> str:
    """Базовый тег входящего движения по мемо: revote / walletverify / stake."""
    if parse_revote_memo(comment) is not None:
        return "revote"
    if parse_verify_memo(comment) is not None:
        return "walletverify"
    return "stake"


def classify_outgoing(comment: str) -> str:
    """Базовый тег исходящего движения по мемо: refund / payout:<kind>."""
    parsed = parse_way_memo(comment)
    if parsed is None:
        return "unknown_out"
    kind, _payout_id = parsed
    return "refund" if kind == "refund" else f"payout:{kind}"