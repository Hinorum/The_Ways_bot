"""Дальняя память мира: старые дни возвращаются, когда похожи на настоящее.

Последние дни и так видны модели (окно канона), а всё, что старше окна,
растворялось в шуме навсегда. Теперь каждый день превращается в лёгкий
хэш-вектор (без нейросетей и внешних API — чистая математика на crc32),
и перед генерацией нового дня мы достаём из глубокого past самые
сюжетно-родственные дни. Они уходят в промпт как «давний канон», и мир
начинает помнить собственную историю неделями позже, чем раньше.
"""

from __future__ import annotations

import math
import re
import zlib
from collections import Counter

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


def _tokens(text: str) -> list[str]:
    return [
        word
        for word in _WORD_RE.findall(text.lower())
        if len(word) > 2 and word not in _STOPWORDS
    ]


def _grams(word: str) -> list[str]:
    """Триграммы слова с граничными метками.

    Символьный уровень делает память слепой к падежам и числам:
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


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def similarity(query: str, text: str) -> float:
    return cosine(embed(query), embed(text))


def recall_beats(
    beats: list[str],
    query: str,
    k: int = 3,
    exclude_recent: int = 12,
    min_score: float = 0.08,
) -> list[str]:
    """Самые сюжетно близкие дни из давнего канона.

    beats — строки канона по порядку («Заголовок: текст»). Последние
    exclude_recent строк исключаются: они и так видны модели в окне.
    Возвращается не больше k строк с похожестью выше порога шума.
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
