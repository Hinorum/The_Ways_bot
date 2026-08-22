"""Утилиты TON: валидация адресов, конвертация нанотон, хук доверия к кошельку."""

from __future__ import annotations

import base64

NANO = 1_000_000_000


def is_valid_ton_address(address: str) -> bool:
    """Принимает user-friendly base64url (EQ/UQ/0Q, 48 символов) и raw hex."""
    candidate = address.strip()
    if len(candidate) == 48 and candidate[:2] in {"EQ", "UQ", "0Q"}:
        try:
            normalized = candidate.replace("-", "+").replace("_", "/")
            decoded = base64.b64decode(normalized, validate=True)
            return len(decoded) == 36
        except Exception:
            return False
    if len(candidate) == 66 and candidate[1] == ":":
        hex_part = candidate[2:]
        try:
            int(hex_part, 16)
            return True
        except ValueError:
            return False
    return False


def normalize_address(address: str) -> str:
    """Приводит к единому виду для сравнения (raw hex без флагов).

    User-friendly раскладка: байт 0 — тег, байт 1 — воркчейн (знаковый),
    байты 2..33 — хеш аккаунта, байты 34..35 — CRC.
    """
    candidate = address.strip()
    if len(candidate) == 48 and candidate[:2] in {"EQ", "UQ", "0Q"}:
        normalized = candidate.replace("-", "+").replace("_", "/")
        decoded = base64.b64decode(normalized, validate=True)
        wc = decoded[1] - 256 if decoded[1] >= 0x80 else decoded[1]
        return f"{wc}:{decoded[2:34].hex()}"
    return candidate.lower()


def to_nano(amount_ton: float) -> int:
    return int(round(amount_ton * NANO))


def from_nano(amount_nanotons: int) -> float:
    return amount_nanotons / NANO


def wallet_trust(address: str) -> dict:
    """Заготовка анти-сибил проверки: возраст кошелька и активность.

    На этапе активации TON здесь будет запрос к tonapi
    (/v2/accounts/{address}/history) с оценкой: свежий пустой кошелёк —
    пониженное доверие (уменьшенный лимит ставки или ручная проверка).
    """
    return {"address": normalize_address(address), "age_days": None, "trusted": True}
