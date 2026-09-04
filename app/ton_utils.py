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


def _crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def friendly_address(raw: str, *, testnet: bool = False, bounceable: bool = False) -> str:
    """Обратное к normalize_address: raw hex -> user-friendly base64url.

    Нужен для показа игроку и платёжных ссылок: хранить адрес в raw удобно
    для сверки, читать его человеку — нет. Тег по умолчанию 0x51
    (non-bounceable); тестнет-бит 0x80 даёт знакомые по кошелькам префиксы
    0Q/kQ. Если raw разобрать не удалось, возвращаем как есть.
    """
    candidate = raw.strip().lower()
    try:
        wc_part, hex_part = candidate.split(":", 1)
        wc = int(wc_part)
        account = bytes.fromhex(hex_part)
    except ValueError:
        return raw
    if len(account) != 32 or not -128 <= wc <= 127 or len(hex_part) != 64:
        return raw
    tag = 0x11 if bounceable else 0x51
    if testnet:
        # Тестнет-бит 0x80: даёт префиксы 0Q/kQ как в тестнет-кошельках.
        tag |= 0x80
    payload = bytes([tag, wc & 0xFF]) + account
    payload += _crc16_xmodem(payload[:34]).to_bytes(2, "big")
    return base64.b64encode(payload).decode().replace("+", "-").replace("/", "_")


def to_nano(amount_ton: float) -> int:
    return int(round(amount_ton * NANO))


def from_nano(amount_nanotons: int) -> float:
    return amount_nanotons / NANO
