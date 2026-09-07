"""Imgbb — бесплатное хранилище картинок (без карты, без кредитки).

Картинки загружаются при генерации и отдаются по публичному URL.
Telegram поддерживает send_photo(url=...) — FSInputFile не нужен.

Если IMGBB_API_KEY не задан — возвращаем None и callers фолбэчатся на локальный диск.

Регистрация: https://api.imgbb.com/ → бесплатный API key (мгновенно).
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

IMGBB_API = "https://api.imgbb.com/1/upload"


def is_configured() -> bool:
    return bool(settings.imgbb_api_key)


async def upload_bytes(data: bytes, name: str = "cover") -> str | None:
    """Upload raw bytes to imgbb, return public URL or None on failure."""
    if not is_configured():
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                IMGBB_API,
                data={
                    "key": settings.imgbb_api_key,
                    "image": base64.b64encode(data).decode(),
                    "name": name,
                },
            )
            if resp.status_code == 200:
                url = resp.json().get("data", {}).get("url")
                if url:
                    return url
            logger.warning("imgbb upload failed: HTTP %d", resp.status_code)
    except Exception:
        logger.exception("imgbb upload exception")
    return None


async def upload_file(path: Path, name: str = "cover") -> str | None:
    """Upload a local file to imgbb, return public URL or None on failure."""
    if not is_configured():
        return None
    try:
        data = path.read_bytes()
        return await upload_bytes(data, name=name)
    except Exception:
        logger.exception("imgbb upload_file failed: %s", path)
        return None
