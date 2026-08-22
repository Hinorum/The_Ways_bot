"""Канон «Эха Стаи»: сцены собираются из прошлых решений без платного API.

Офлайн-фолбэк: если нейросеть молчит, глава дня собирается здесь. Правило
стиля — простой ясный русский язык и никакой одной фразы во всех трёх картах:
каждая карта описывает своё действие целиком.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, replace


WORLD_BIBLE = (
    "Мир называется Эхо Стаи. После Последнего Пути стая потерянных собак "
    "нашла сеть нестабильных миров-порталов: каждый мир собран из чужих "
    "решений. Один день — один выбор на всю стаю, и реальность перестраивается "
    "под итог. Где-то в глубине сети звучит Первый Лай: дорога домой или "
    "приманка Хозяина Ошибки — не знает никто."
)


@dataclass(frozen=True)
class CardDraft:
    title: str
    description: str
    consequence: str
    tag: str
    image_prompt: str


def _rng(day_index: int, salt: str) -> random.Random:
    seed = int(hashlib.sha256(f"{day_index}:{salt}".encode()).hexdigest()[:16], 16)
    return random.Random(seed)


def tags_from_beats(previous_beats: list[str]) -> list[str]:
    tags: list[str] = []
    mapping = {
        "лай": "risk",
        "искр": "risk",
        "зуб": "risk",
        "глаз": "risk",
        "голод": "risk",
        "портал": "cunning",
        "архив": "cunning",
        "мутант": "cunning",
        "глюк": "cunning",
        "имя": "cunning",
        "кост": "care",
        "стая": "care",
        "дом": "care",
        "тепл": "care",
        "лун": "care",
        "сон": "care",
    }
    for beat in previous_beats:
        low = beat.lower()
        for needle, tag in mapping.items():
            if needle in low:
                tags.append(tag)
                break
        else:
            tags.append("care")
    return tags


# Места дня упоминаются в тексте главы один раз; карты остаются
# самостоятельными действиями без вставки одного и того же топонима.
_PLACES = [
    {
        "to": "к порталу на окраине — он гудит и лает изнутри",
        "scene": "pack of stray dogs facing a humming portal on a foggy city outskirts",
    },
    {
        "to": "в заброшенный приют, где миски ещё тёплые",
        "scene": "abandoned dog shelter with glowing warm bowls and flickering light",
    },
    {
        "to": "на станцию Архива, где папки шепчут чужие имена",
        "scene": "endless archive hall with whispering folders and paper dust in light beams",
    },
    {
        "to": "в город без теней — солнце светит, но тени не ложатся",
        "scene": "sunny empty city where dogs cast no shadows, uncanny calm streets",
    },
    {
        "to": "на мост из светящихся костей между двумя мирами",
        "scene": "bridge built of glowing bones spanning two different skies",
    },
    {
        "to": "в пустой вольер Нулевого Блока с погасшими лампами",
        "scene": "empty concrete kennel block with dead lamps and a red standby dot",
    },
    {
        "to": "на рынок Лайнеров, где торгуют чужими снами",
        "scene": "night market stalls selling bottled dreams, hooded traders, lanterns",
    },
    {
        "to": "к реке, которая течёт вспять по памяти",
        "scene": "river flowing backwards through a misty meadow with floating photos",
    },
]

# Заголовок, описание и последствие связаны намертво: карта называет то,
# что делает, и последствие вытекает именно из этого действия. Никаких
# общих вставных фраз между картами.
_RISK_PATHS = [
    (
        "Первый вход",
        "Войти в портал первым, пока он не начал закрываться. Кто входит первым — тот задаёт тон целому миру.",
        "Стая вошла в мир, где небо двоится. Канон отмечает: обратный портал открылся с той стороны.",
    ),
    (
        "Красный сигнал",
        "Лаять на мигающий красным источник сигнала в открытую и потребовать ответа сейчас, а не завтра.",
        "Источник сигнала ответил раньше срока. В каноне появилась трещина, которой вчера не было.",
    ),
    (
        "Голодный портал",
        "Пойти туда, где пахнет палёной проводкой, и выяснить, что там гудит.",
        "Гул стих, но у одной из собак теперь светятся глаза. Канон это запомнил.",
    ),
    (
        "Прорыв",
        "Перепрыгнуть через барьер Нулевого Блока до того, как датчики проснутся.",
        "Датчики промолчали. Канон зафиксировал первый незарегистрированный вход в Нулевой Блок.",
    ),
    (
        "Кабель в зубах",
        "Схватить зубами мигающий кабель и держать, пока вся стая проходит вперёд.",
        "Кабель удержался. Мир получил короткое замыкание, а стая — лишний час форы.",
    ),
]
_CARE_PATHS = [
    (
        "Кость стаи",
        "Остаться и разделить еду поровну, даже если на всех выходит по паре кусков.",
        "Никто не ушёл голодным. В каноне появилось место, где еду делят без счёта.",
    ),
    (
        "Тёплые миски",
        "Наполнить миски раньше похода к порталу: сытые решают лучше голодных.",
        "Миски опустели к рассвету. Канон отметил: сильна та стая, где никто не ходит голодным.",
    ),
    (
        "Общая будка",
        "Снести будки в один лагерь: вместе теплее, а стены из чужих миров не спасают.",
        "Лагерь стал общим. Канон отметил: стены теперь не делят стаю.",
    ),
    (
        "Сон вповалку",
        "Уложить самых усталых ближе к теплу и спать одной кучей до рассвета.",
        "Ночь прошла спокойно. Уставшие вспомнили имена друг друга — канон стал теплее.",
    ),
    (
        "Мокрый нос рядом",
        "Вылизать рану тому, кто вчера ошибся, и не спрашивать его ни о чём.",
        "Рана затянулась. Вчерашний промах больше ни при чём: канон вычеркнул обиду.",
    ),
]
_CUNNING_PATHS = [
    (
        "Чужое имя",
        "Пройти под чужим именем из архива: след поведёт не к тебе.",
        "Архив поверил чужому имени. Канон завёл страницу, которая ведёт не туда — и это удобно.",
    ),
    (
        "Обходной след",
        "Сделать вид, что портал не интересует стаю, и обойти его тихим двором.",
        "Портал остался нетронутым. Канон добавил тропу двором, которой нет на схемах.",
    ),
    (
        "Тихий лаз",
        "Подменить метку на ошейнике и отправить вместо себя двойника из глючного мира.",
        "Двойник ушёл в портал вместо игрока. Хозяин Ошибки пересчитал стаю и сбился со счёта.",
    ),
    (
        "Сделка с архивариусом",
        "Торговаться с Лайнером: отдать скучное воспоминание за карту прохода.",
        "Проход куплен честно по меркам Лайнеров. В каноне появился долг: маленький, но чужой.",
    ),
    (
        "Ложный лай",
        "Записать в архив ложную версию дня — пусть Хозяин Ошибки её переваривает.",
        "Ложная версия легла в архив гладко. Канон предупреждает: однажды она окажется правдой.",
    ),
]

_IMAGE_PROMPTS = {
    "risk": (
        "stray dog stepping into a humming glitching portal, sparks and static, "
        "dark fairy-tale digital painting, dramatic rim light, teal and violet palette, no text"
    ),
    "care": (
        "stray dogs sleeping close together around warm bowls, soft amber glow, "
        "dark fairy-tale digital painting, gentle light in the dark, no text"
    ),
    "cunning": (
        "stray dog wearing another dog's name tag, mirrored portal behind, sly pose, "
        "dark fairy-tale digital painting, teal and violet palette, no text"
    ),
}


def compose_chapter(day_index: int, previous_beats: list[str], win_rule=None, echoes=None) -> dict:
    rng = _rng(day_index, "|".join(previous_beats[-5:]))
    history_tags = tags_from_beats(previous_beats)
    last = previous_beats[-1] if previous_beats else None
    echo = _echo(last, history_tags)
    place = _PLACES[(day_index + len(history_tags)) % len(_PLACES)]
    cover_prompt = (
        "wide cinematic establishing shot, " + place["scene"]
        + ", dark fairy-tale digital painting, glow of an open portal, "
        "teal and violet palette, volumetric fog, cinematic composition, no text, no letters"
    )
    title_bits = [
        "Портал лает",
        "Тёплые миски",
        "Имя не твоё",
        "Стая слышит",
        "Сигнал отвечает",
        "Город без теней",
        "Мост из костей",
        "Первый Лай ближе",
    ]
    cards = _cards(rng, day_index)
    active_echoes = list(echoes or [])
    text = _chapter_text(day_index, echo, place, history_tags, last, win_rule)
    if active_echoes:
        touches = (
            "На обочине снова мелькает «{name}».",
            "«{name}» не отстаёт от тропы.",
            "Где-то за спиной остаётся «{name}».",
        )
        primary = active_echoes[0]
        idx = rng.randrange(len(cards))
        target = cards[idx]
        cards[idx] = replace(
            target,
            consequence=(
                f"{target.consequence} "
                + touches[rng.randrange(len(touches))].format(name=primary.title)
            ),
        )
        for item in active_echoes:
            sentence = item.description.strip()
            if not sentence or sentence in text:
                # Эхо часто дословно повторяет недавний канон — не дублируем.
                continue
            if sentence[-1] not in ".!?…":
                sentence += "."
            text += f" {sentence}"
    return {
        "title": f"День {day_index}. {title_bits[(day_index - 1) % len(title_bits)]}",
        "text": text,
        "lore_summary": echo,
        "cover_prompt": cover_prompt,
        "cards": [
            {
                "title": card.title,
                "description": card.description,
                "consequence": card.consequence,
                "tag": card.tag,
                "image_prompt": card.image_prompt,
            }
            for card in cards
        ],
    }


def _echo(last: str | None, tags: list[str]) -> str:
    if not last:
        return "Стая впервые стоит перед сетью порталов. Ни одного решения сюда ещё не впечатано."
    dominant = max(set(tags), key=tags.count) if tags else "care"
    tone = {
        "risk": "Сеть стала резче: порталы гудят громче, чем вчера.",
        "care": "Сеть стала тише: стая помнит вчерашний выбор и держится кучнее.",
        "cunning": "Сеть стала хитрее: ветки реальности путаются на ровном месте.",
    }[dominant]
    title, _, deed = last.partition(":")
    title = title.strip() or "вчерашний путь"
    deed = deed.strip()
    if len(deed) > 180:
        deed = deed[:177] + "…"
    if deed and deed[-1] not in ".!?…":
        deed += "."
    if deed:
        return f"{tone} Вчера стая выбрала «{title}» — {deed} Мир перестроился под этот выбор."
    return f"{tone} Вчера стая выбрала «{title}», и сеть перестроилась под этот шаг."


def _chapter_text(
    day_index: int,
    echo: str,
    place: dict,
    tags: list[str],
    last: str | None,
    win_rule=None,
) -> str:
    rng = _rng(day_index, "closing:" + "|".join(tags))
    law_line = ""
    if win_rule is not None:
        from app.models import RULE_PHRASES

        law_line = f"Закон дня объявлен с утра: {RULE_PHRASES[win_rule]}. "

    if day_index == 1:
        text = (
            f"Стая вышла к первой развилке. Впереди гудит портал — не светится и не мерцает, "
            f"а просто смотрит. На рассвете вся сеть на секунду замолчала, и в этой тишине "
            f"все собаки разом услышали Первый Лай."
        )
        # После сброса с сохранением истории мир помнит старый канон.
        if last:
            old_title = last.split(":")[0].strip()
            text += f" Счёт обнулён, но память сети жива: однажды стая уже выбирала «{old_title}»."
        if win_rule is not None:
            text += f" {law_line}Счёт скрыт до итогов: выбирай то, что готова пережить стая. "
        else:
            text += " Закон первого дня объявят утром. "
        return text + "Одна карта на всех. Завтра мир будет другим."

    motif = "порталы сегодня спокойны"
    if tags.count("risk") >= tags.count("care"):
        motif = "порталы гудят заметно громче обычного"
    if tags.count("cunning") >= 2:
        motif = "карты в архивах меняют формулировки сами собой"

    yesterday = last.split(":")[0].strip() if last else "первый день"
    closings = [
        (
            "До закрытия развилки сутки. Счёт скрыт до итогов, закон известен с утра. "
            "Что бы ни выбрала стая, к вечеру это станет общей памятью сети — "
            "и завтра придётся жить уже в этом мире."
        ),
        (
            "Выбор один на всех и сутки на раздумья. Цифры спрятаны, закон объявлен. "
            "Какую карту ни возьмёт стая, сеть впечатает решение в себя — "
            "и утром оно станет обычной жизнью."
        ),
        (
            "Развилка ждёт ровно сутки. Счёт скрыт, закон известен. Победивший вариант "
            "станет частью мира быстрее, чем высохнут миски, — так здесь работает память."
        ),
        (
            "Стая садится вокруг и смотрит на три карты. Кто-то нюхает воздух, кто-то — чужие ошейники. "
            "Сутки на решение; закон дня объявлен с самого утра, счёт покажут только в итогах."
        ),
        (
            "Дальше путь расходится трижды — такое бывает только в сети. "
            "Один голос на всех, сутки времени, скрытый счёт и известный закон. "
            "Вечером сеть перепишет под итог хотя бы пару мелочей."
        ),
    ]
    return (
        f"{echo} Сегодня стая идёт {place['to']}. "
        f"Приметы вчерашнего решения видны прямо здесь: {motif}. {law_line}"
        + closings[rng.randrange(len(closings))]
    )


def _cards(rng: random.Random, day_index: int) -> list[CardDraft]:
    """Карты дня без повторов на соседних сутках.

    Тройка «заголовок—описание—последствие» выбирается целиком по циклу дня
    со сдвигом на тег — карта не может выпасть два дня подряд, а последствие
    всегда вытекает из своего действия. rng остаётся только на порядок карт.
    """

    def pick(paths: list[tuple[str, str, str]], offset: int) -> CardDraft:
        title, description, consequence = paths[(day_index + offset) % len(paths)]
        return (title, description, consequence)

    risk_title, risk_desc, risk_conseq = pick(_RISK_PATHS, 0)
    care_title, care_desc, care_conseq = pick(_CARE_PATHS, 2)
    cunning_title, cunning_desc, cunning_conseq = pick(_CUNNING_PATHS, 4)
    cards = [
        CardDraft(
            title=risk_title,
            description=risk_desc,
            consequence=risk_conseq,
            tag="risk",
            image_prompt=_IMAGE_PROMPTS["risk"],
        ),
        CardDraft(
            title=care_title,
            description=care_desc,
            consequence=care_conseq,
            tag="care",
            image_prompt=_IMAGE_PROMPTS["care"],
        ),
        CardDraft(
            title=cunning_title,
            description=cunning_desc,
            consequence=cunning_conseq,
            tag="cunning",
            image_prompt=_IMAGE_PROMPTS["cunning"],
        ),
    ]
    rng.shuffle(cards)
    return cards
