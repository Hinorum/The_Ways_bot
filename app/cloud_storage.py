"""Imgbb — бесплатное хранилище картинок (без карты, без кредитки).

Картинки загружаются при генерации и отдаются по публичному URL.
Telegram поддерживает send_photo(url=...) — FSInputFile не нужен.

Если IMGBB_API_KEY не задан — возвращаем None и callers фолбэчатся на локальный диск.

Регистрация: https://api.imgbb.com/ → бесплатный API key (мгновенно).
"""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

IMGBB_API = "https://api.imgbb.com/1/upload"
_MAX_ATTEMPTS = 3
_RETRY_DELAY = 1.5


def is_configured() -> bool:
    return bool(settings.imgbb_api_key)


async def upload_bytes(data: bytes, name: str = "cover") -> str | None:
    """Upload raw bytes to imgbb, return public URL or None on failure."""
    if not is_configured():
        return None
    attempt = 0
    while attempt < _MAX_ATTEMPTS:
        attempt += 1
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
                logger.warning("imgbb upload failed: HTTP %d (attempt %d/%d)", resp.status_code, attempt, _MAX_ATTEMPTS)
        except Exception:
            logger.exception("imgbb upload exception (attempt %d/%d)", attempt, _MAX_ATTEMPTS)
        if attempt < _MAX_ATTEMPTS:
            await asyncio.sleep(_RETRY_DELAY * attempt)
    return None


async def upload_file(path: Path, name: str = "cover") -> str | None:
    """Upload a local file to imgbb, return public URL or None on failure."""
    if not is_configured():
        return None
    try:
        data = await asyncio.to_thread(path.read_bytes)
        return await upload_bytes(data, name=name)
    except Exception:
        logger.exception("imgbb upload_file failed: %s", path)
        return None
