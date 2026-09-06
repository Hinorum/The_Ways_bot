"""Cloudflare R2 (S3-совместимый) для хранения сгенерированных картинок.

Картинки загружаются в R2 при генерации и отдаются по публичному URL.
Telegram поддерживает send_photo(url=...) — FSInputFile не нужен.

Если R2 не настроен — возвращаем None и callers фолбэчатся на локальный диск.
"""

from __future__ import annotations

import io
import logging
import mimetypes
from pathlib import Path

import boto3
from botocore.config import Config

from app.config import settings

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not settings.r2_account_id or not settings.r2_access_key_id:
        return None
    _client = boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        config=Config(
            retries={"max_attempts": 3, "mode": "adaptive"},
            signature_version="s3v4",
        ),
    )
    return _client


def is_configured() -> bool:
    return bool(settings.r2_account_id and settings.r2_access_key_id and settings.r2_bucket_name)


def _public_url(key: str) -> str:
    base = settings.r2_public_url.rstrip("/")
    return f"{base}/{key}"


async def upload_image(data: bytes, key: str, content_type: str = "image/jpeg") -> str | None:
    """Upload bytes to R2, return public URL or None on failure."""
    client = _get_client()
    if client is None:
        return None
    try:
        client.put_object(
            Bucket=settings.r2_bucket_name,
            Key=key,
            Body=data,
            ContentType=content_type,
            CacheControl="public, max-age=86400",
        )
        return _public_url(key)
    except Exception:
        logger.exception("R2 upload failed: %s", key)
        return None


async def upload_file(path: Path, key: str) -> str | None:
    """Upload a local file to R2, return public URL or None on failure."""
    client = _get_client()
    if client is None:
        return None
    ct = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    try:
        client.upload_file(
            str(path),
            settings.r2_bucket_name,
            key,
            ExtraArgs={"ContentType": ct, "CacheControl": "public, max-age=86400"},
        )
        return _public_url(key)
    except Exception:
        logger.exception("R2 upload_file failed: %s", key)
        return None


def make_key(*parts: str) -> str:
    """Build a deterministic R2 object key from path parts."""
    return "/".join(p.strip("/") for p in parts if p)
