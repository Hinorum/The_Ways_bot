"""Дальняя память мира: старые дни возвращаются, когда похожи на настоящее.

Улучшенная архитектура: опциональный embedding-слой с API (OpenAI/DeepSeek)
поверх существующего hashing-trick. Если API недоступен — используется crc32.

Алгоритм:
1. Если настроен API-ключ → семантические эмбеддинги (качество выше)
2. Иначе → hashing trick (крч32, работает без API)
3. Кэш API-эмбеддингов в памяти (避免重复调用)
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import zlib
from collections import Counter
from typing import Any

_DIM = 1024

_WORD_RE = re.compile(r"[а-яёa-z0-9]+")

# Служебные слова русского языка не должны управлять памятью мира.
_STOPWORDS = frozenset(
    """
    и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по
    только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли
    если уже или ни быть был него до вас нибудь опять уж вам ведь там потом себя
    ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без
    будто чего раз тоже себе под будет ж тогда кто этот потому этого какой совсем
    здесь этом один почти мой тем чтобы нее кажется сейчас были куда зачем всех
    никогда сегодня можно при наконец два об другой хоть после над больше тот
    через эти нас про всего них какая много три эту моя хорошо свою этой перед
    иногда лучше чуть том нельзя такой им более всегда конечно всю между очень
    это этот эта эти ними нами вас них нее ими том там где-то что-то какой-то
    какой-нибудь какой либо во-первых во-вторых тд тп
    """.split()
)

# ── Кэш API-эмбеддингов с TTL ──
_embedding_cache: dict[str, tuple[list[float], float]] = {}
_EMBED_CACHE_TTL = 3600  # 1 час


def _cache_get(key: str) -> list[float] | None:
    """Читает кэш с проверкой TTL."""
    import time

    entry = _embedding_cache.get(key)
    if entry is not None:
        vec, ts = entry
        if time.monotonic() - ts < _EMBED_CACHE_TTL:
            return vec
        del _embedding_cache[key]
    return None


def _cache_put(key: str, vec: list[float]) -> None:
    """Записывает в кэш с текущим timestamp."""
    import time

    _embedding_cache[key] = (vec, time.monotonic())
    # Простая eviction: если кэш > 10000, чистим старые
    if len(_embedding_cache) > 10_000:
        now = time.monotonic()
        stale = [k for k, (_, ts) in _embedding_cache.items() if now - ts > _EMBED_CACHE_TTL]
        for k in stale:
            del _embedding_cache[k]


def _tokens(text: str) -> list[str]:
    return [
        word
        for word in _WORD_RE.findall(text.lower())
        if len(word) > 2 and word not in _STOPWORDS
    ]


def _grams(word: str) -> list[str]:
    """Триграммы слова с граничными метками.

    Символьный уровень делает память слабой к падежам и числам:
    «перевале» и «перевал» почти целиком состоят из одних триграмм,
    «волчья» и «волк» делят начало слова — этого достаточно, чтобы
    сюжетно близкие дни узнавали друг друга.
    """
    padded = f"^{word}$"
    return [padded[start : start + 3] for start in range(max(1, len(padded) - 2))]


def embed(text: str) -> list[float]:
    """Детерминированный вектор текста: hashing trick по триграммам + tf.

    crc32 вместо встроенного hash() — стабильность между процессами
    (PYTHONHASHSEED меняет обычные хэши строк при каждом запуске).
    """
    vector = [0.0] * _DIM
    counts: Counter[str] = Counter()
    for word in _tokens(text):
        counts.update(_grams(word))
    for gram, count in counts.items():
        weight = 1.0 + math.log(count)
        h1 = zlib.crc32(gram.encode("utf-8"))
        h2 = zlib.crc32(("~" + gram).encode("utf-8"))
        idx1 = h1 % _DIM
        sign1 = 1.0 if (h1 >> 16) & 1 else -1.0
        idx2 = h2 % _DIM
        sign2 = 1.0 if (h2 >> 16) & 1 else -1.0
        vector[idx1] += sign1 * weight
        if idx2 != idx1:
            vector[idx2] += sign2 * weight
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0.0:
        return vector
    return [component / norm for component in vector]


async def _api_embed(text: str) -> list[float] | None:
    """Семантический эмбеддинг через API (OpenAI-compatible).

    Возвращает None если API недоступен или текст пустой.
    Кэширует результаты по хэшу текста с TTL 1 час.
    """
    if not text.strip():
        return None

    cache_key = hashlib.sha256(text.encode()).hexdigest()[:32]
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        from app.config import settings

        api_key = getattr(settings, "openai_api_key", None)
        base_url = getattr(settings, "openai_base_url", None)
        embed_model = getattr(settings, "embed_model", None)

        if not api_key or not embed_model:
            return None

        import httpx

        url = f"{base_url or 'https://api.openai.com/v1'}/embeddings"
        headers = {"Authorization": f"Bearer {api_key}"}
        payload = {"model": embed_model, "input": text[:8000]}  # truncate for safety

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            vector = data["data"][0]["embedding"]

            # Нормализуем до _DIM если размер отличается
            if len(vector) != _DIM:
                vector = _normalize_to_dim(vector, _DIM)

            _cache_put(cache_key, vector)
            return vector
    except Exception:
        return None


def _normalize_to_dim(vector: list[float], dim: int) -> list[float]:
    """Проецирует вектор в нужную размерность (усреднение/разбиение)."""
    if len(vector) == dim:
        return vector
    if len(vector) > dim:
        # Усредняем группы
        chunk_size = len(vector) // dim
        result = []
        for i in range(dim):
            chunk = vector[i * chunk_size : (i + 1) * chunk_size]
            result.append(sum(chunk) / len(chunk))
        return result
    # Дополняем нулями
    return vector + [0.0] * (dim - len(vector))


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def similarity(query: str, text: str) -> float:
    return cosine(embed(query), embed(text))


async def semantic_recall(
    beats: list[str],
    query: str,
    k: int = 3,
    exclude_recent: int = 12,
    min_score: float = 0.08,
) -> list[str]:
    """Семантический поиск по истории: API эмбеддинги + fallback на hashing trick.

    Если API доступен и вернул эмбеддинги — использует их.
    Иначе — fallback на существующий hashing trick.
    """
    if len(beats) <= exclude_recent or not query.strip():
        return []

    ancient = beats[:-exclude_recent]

    # Пробуем API-эмбеддинги
    query_vec = await _api_embed(query)
    if query_vec is not None:
        # Запоминаем индексы ancient beats, для которых есть API-эмбеддинги
        api_vectors: list[tuple[int, list[float]]] = []
        for i, beat in enumerate(ancient):
            vec = await _api_embed(beat)
            if vec is not None:
                api_vectors.append((i, vec))

        if len(api_vectors) >= 3:  # Достаточно данных для сравнения
            scored = sorted(
                ((cosine(query_vec, vec), idx) for idx, vec in api_vectors),
                key=lambda pair: pair[0],
                reverse=True,
            )
            return [
                ancient[idx]
                for score, idx in scored[:k]
                if score >= min_score
            ]

    # Fallback: hashing trick (существующий алгоритм)
    query_vector = embed(query)
    scored = sorted(
        ((cosine(query_vector, embed(beat)), beat) for beat in ancient),
        key=lambda pair: pair[0],
        reverse=True,
    )
    return [beat for score, beat in scored[:k] if score >= min_score]


def recall_beats(
    beats: list[str],
    query: str,
    k: int = 3,
    exclude_recent: int = 12,
    min_score: float = 0.08,
) -> list[str]:
    """Синхронная версия: hashing trick (без API).

    Оставлен для обратной совместимости. Новые модули должны
    использовать semantic_recall().
    """
    if len(beats) <= exclude_recent or not query.strip():
        return []
    ancient = beats[:-exclude_recent]
    query_vector = embed(query)
    scored = sorted(
        ((cosine(query_vector, embed(beat)), beat) for beat in ancient),
        key=lambda pair: pair[0],
        reverse=True,
    )
    return [beat for score, beat in scored[:k] if score >= min_score]
