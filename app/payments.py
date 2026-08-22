"""Платная смена выбора: форматы payload-ов и мемо, общие для Stars и TON."""

from __future__ import annotations

REVOTE_MEMO_PREFIX = "rv:"


def revote_memo(round_id: int) -> str:
    """Комментарий к TON-переводу за смену выбора."""
    return f"{REVOTE_MEMO_PREFIX}{round_id}"


def build_revote_payload(round_id: int) -> str:
    """invoice_payload для счёта Telegram Stars."""
    return f"revote:{round_id}"


def parse_revote_payload(payload: str | None) -> int | None:
    parts = (payload or "").split(":")
    if len(parts) == 2 and parts[0] == "revote" and parts[1].isdigit():
        return int(parts[1])
    return None


def parse_revote_memo(memo: str | None) -> int | None:
    text = (memo or "").strip().lower()
    if not text.startswith(REVOTE_MEMO_PREFIX):
        return None
    raw = text[len(REVOTE_MEMO_PREFIX):].strip()
    return int(raw) if raw.isdigit() else None
