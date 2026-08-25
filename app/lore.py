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


# Короткие имена мест маршрута — по индексу совпадают с _PLACES. Память о
# географии позволяет возвращать стаю туда, где уже всё изменилось.
_PLACE_NAMES = [
    "Окраина тумана",
    "Старый приют",
    "Бесконечный архив",
    "Город без теней",
    "Мост над развязкой",
    "Река мёртвых порталов",
    "Ярмарка Лайнеров",
    "Гнездо Первого Лая",
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


def compose_chapter(
    day_index: int,
    previous_beats: list[str],
    win_rule=None,
    echoes=None,
    distant_echoes: list[str] | None = None,
    season_block: str | None = None,
    villain_line: str | None = None,
    sealed: bool = False,
    pending_outcome: bool = False,
    salt: str = "",
    tint_lines: list[str] | None = None,
    focus_line: str | None = None,
) -> dict:
    # Соль запуска: каждый сброс/перезапуск даёт свежие комбинации
    # закрывок, карт дня и вступлений вместо жёсткой арифметики дня.
    rng = _rng(day_index, f"{salt}|{'|'.join(previous_beats[-5:])}")
    history_tags = tags_from_beats(previous_beats)
    last = previous_beats[-1] if previous_beats else None
    # Фаза 1 прегенерации: итог «вчера» ещё не вскрыт — глава начинается сразу
    # со сцены, а отголосок допишет patch_prepared_day после итогов.
    echo = "" if pending_outcome else _echo(last, history_tags)
    place_idx = (day_index + len(history_tags)) % len(_PLACES)
    place = _PLACES[place_idx]
    place_name = _PLACE_NAMES[place_idx]
    is_finale = bool(season_block and "ДЕНЬ ПЕРВОГО ЛАЯ" in season_block)
    cover_prompt = (
        "wide cinematic establishing shot, " + place["scene"]
        + ", dark fairy-tale digital painting, glow of an open portal, "
        "volumetric fog, cinematic composition, no text, no letters"
    )
    if is_finale:
        title = f"День {day_index}. Первый Лай"
        text = (
            f"Стая выходит {place['to']}. Сегодня порталы молчат все до одного — "
            "и в этой тишине Лай звучит изнутри костей. Он зовёт каждого по-своему: "
            "кто-то слышит тёплый двор, кто-то ловушку, чей-то нос чует, что зов "
            "можно перехватить. Три тропы расходятся от последнего портала."
        )
        cards = [
            replace(
                card,
                consequence=(
                    f"Стая выбрала «{card.title}» — и Первый Лай замолкает, "
                    "оставив мир другим."
                ),
            )
            for card in _finale_cards(rng)
        ]
    else:
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
        title = f"День {day_index}. {title_bits[(day_index - 1) % len(title_bits)]}"
        cards = _cards(rng, day_index)
        active_echoes = list(echoes or [])
        text = _chapter_text(
            day_index, echo, place, history_tags, last, win_rule,
            sealed=sealed, salt=salt, rng=rng,
        )
        if villain_line:
            # План Хозяина Ошибки: в текст уходит только каноническое событие
            # (строка списка «- …»), а не служебные строки блока промпта.
            event_lines = [
                line.lstrip("- ").strip()
                for line in villain_line.rstrip().splitlines()
                if line.strip().startswith("-")
            ]
            last_event = event_lines[-1].strip() if event_lines else ""
            if last_event and last_event not in text:
                if last_event[-1] not in ".!?…":
                    last_event += "."
                text += f" {last_event}"
        if active_echoes:
            touches = (
                "На обочине примостилось «{name}».",
                "«{name}» не отстаёт от тропы.",
                "Кто-то оставил у портала «{name}».",
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
                    # Дословный повтор не добавляем: след должен угадываться.
                    continue
                if sentence[-1] not in ".!?…":
                    sentence += "."
                text += f" {sentence}"
        distant_variants = (
            "Давний след всплывает сам собой: {snippet}",
            "Из глубины канона проступает: {snippet}",
            "Старая история догоняет стаю: {snippet}",
        )
        for distant in distant_echoes or []:
            snippet = " ".join(distant.split())
            if not snippet or snippet in text:
                continue
            if len(snippet) > 160:
                snippet = snippet[:157] + "…"
            if snippet[-1] not in ".!?…":
                snippet += "."
            text += " " + distant_variants[rng.randrange(len(distant_variants))].format(snippet=snippet)
        # Нрав стаи: тинты характера (офлайн-фолбэк не остаётся безликим).
        for tint in tint_lines or []:
            sentence = " ".join(tint.split())
            if not sentence:
                continue
            if sentence[-1] not in ".!?:…":
                sentence += "."
            if sentence not in text:
                text += f" {sentence}"
        # Фокус-день NPC: одна «хотелка» репликой или жестом.
        if focus_line:
            line = " ".join(focus_line.split())
            if line[-1] not in ".!?…":
                line += "."
            if line not in text:
                text += f" {line}"

    return {
        "title": title,
        "place": place_name,
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


def _finale_cards(rng: random.Random) -> list[CardDraft]:
    """Три прочтения Первого Лая: дом (care), ловушка (risk), стать зовом (cunning)."""
    base = {
        "care": (
            "Дверь в старый двор",
            "Идти на тёплую часть зова: там пахнет мисками и чьим-то ожиданием.",
            "Лай был домом — но за домом придётся платить памятью о тропе.",
        ),
        "risk": (
            "Клыки на зов",
            "Встретить Лай оскалом: если это охота — охота закончится здесь.",
            "Лай был ловушкой — стая сломала капкан, но шум привёл Хозяина Ошибки.",
        ),
        "cunning": (
            "Перехватить зов",
            "Не отвечать, а подстроиться под гул: пусть сеть лает их голосом.",
            "Стая стала зовом — теперь миры приходят к ним сами.",
        ),
    }
    order = ["risk", "care", "cunning"]
    rng.shuffle(order)
    return [
        CardDraft(
            title=base[tag][0],
            description=base[tag][1],
            consequence=base[tag][2],
            tag=tag,
            image_prompt=(
                "dark fairy-tale tarot, first bark interpretation, "
                "stray dog before the source of a glowing call, no text"
            ),
        )
        for tag in order
    ]




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


def _law_voice(win_rule) -> str:
    """Закон дня репликой Хранителя Спорных Версий — даже без нейросети."""
    from app.models import RULE_PHRASES, WinRule

    voices = {
        WinRule.MAJORITY: (
            "«Архив сегодня признаёт большинство», — объявляет Хранитель Спорных "
            "Версий и лениво перелистывает папку. «Громкие лапы ведут»."
        ),
        WinRule.MINORITY: (
            "«Сегодня архив слушает тишину», — шепчет Хранитель Спорных Версий. "
            "«Меньше голосов — сильнее след. Не благодарите»."
        ),
        WinRule.MEDIAN: (
            "«Середина знает меру», — роняет Хранитель Спорных Версий и захлопывает "
            "папку ровно посередине. «Крайности пусть подождут»."
        ),
    }
    voice = voices.get(win_rule)
    if voice is None:
        return f"Закон дня объявлен с утра: {RULE_PHRASES[win_rule]}. "
    return f"{voice} Закон дня: {RULE_PHRASES[win_rule]}. "


_SEAL_VOICE = (
    "«Сегодня архив запечатал урну до вечера», — говорит Хранитель Спорных Версий, "
    "и в его шёпоте слышна улыбка. «Какой сегодня закон — узнаете вместе с итогами. "
    "Если доживёте до итогов». Закон дня скрыт: стая гадает, спорит и принюхивается. "
)


def _chapter_text(
    day_index: int,
    echo: str,
    place: dict,
    tags: list[str],
    last: str | None,
    win_rule=None,
    sealed: bool = False,
    salt: str = "",
    rng: random.Random | None = None,
) -> str:
    if rng is None:
        rng = _rng(day_index, f"{salt}|closing:{'|'.join(tags)}")
    if sealed:
        law_line = _SEAL_VOICE
    else:
        law_line = _law_voice(win_rule) if win_rule is not None else ""

    if day_index == 1:
        # Пул вступлений: каждый перезапуск начинает сезон по-разному.
        openings = (
            (
                "Стая вышла к первой развилке. Впереди гудит портал — не светится и не мерцает, "
                "а просто смотрит. На рассвете вся сеть на секунду замолчала, и в этой тишине "
                "все собаки разом услышали Первый Лай. Стежка первой прижала уши, Вектор — первый "
                "зарычал: стая ещё не знает, что этот звук будет с ними до самого конца сезона."
            ),
            (
                "Утро началось с чужого эха в мисках: вода дрожала в такт чему-то далёкому. "
                "Стая вышла на запах озона — и упёрлась в первую развилку. Портал впереди гудит "
                "негромко, терпеливо. А из его глубины, сквозь слои миров, доносится Первый Лай: "
                "Безымянная первой повернула морду на звук, и все поняли — обратной дороги нет."
            ),
            (
                "Туман сегодня стоял до полудня, а когда спал — за ним открылся портал там, где "
                "вчера была только стена. Стая обнюхала порог кругом: Баркод чихнул, Пиксель "
                "заворожено ловил лапой искры. И тогда изнутри портала прозвучал Первый Лай — "
                "не громко, но так, что у всех зачесались лопатки: впереди целый сезон троп."
            ),
            (
                "Стая проснулась от тишины — той самой, какой не бывает в сети порталов. "
                "Даже ветер замер. Потом сеть вздохнула, и первый же вдох принёс Первый Лай. "
                "Вектор зарычал на пустоту, Стежка прижала уши, а развилка уже ждала: три тропы "
                "разошлись от порога, и каждая пахла чужим решением."
            ),
        )
        text = openings[rng.randrange(len(openings))]
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

    closings = [
        (
            "До закрытия развилки сутки. Архив уже завёл папку этого дня и лениво "
            "ждёт, чем её заполнят."
        ),
        (
            "Сутки на раздумья. Какая бы тропа ни выиграла, к утру она станет "
            "обычностью — так здесь работает память."
        ),
        (
            "Безымянная легла мордой к картам раньше всех. Собаки чувствуют "
            "развилки раньше нас — осталось выбрать."
        ),
        (
            "Баркод тихо скулит на все три карты сразу. Даже он понимает: "
            "выбор один на всех."
        ),
        (
            "Порталы гудят на грани слышимости — сеть затаила дыхание и ждёт "
            "твоего шага."
        ),
        (
            "Ветер принёс запах чужого костра: кто-то уже готовит место под то, "
            "что стая решит."
        ),
        (
            "Пиксель ловит лапой цифровой дождь — мир нервничает не меньше стаи."
        ),
        (
            "Лайнер пересчитывает выручку чужих снов и усмехается краем морды: "
            "он знает цену ожидания."
        ),
        (
            "Миски расставлены полукругом, карты лежат в центре — так стая "
            "решала ещё до порталов."
        ),
        (
            "Вектор обошёл карты по кругу и не тронул ни одну: даже упрямство "
            "умеет ждать."
        ),
        (
            "Стежка чует еду раньше звука, но сегодня звук сильнее: развилка "
            "открыта ровно на сутки."
        ),
        (
            "Где-то в папках уже подписана страница, которой вчера не "
            "существовало. К утру она станет правдой."
        ),
    ]
    head = f"{echo} " if echo else ""
    motif_line = (
        f"Приметы вчерашнего решения видны прямо здесь: {motif}. "
        if echo
        else f"Приметы последних троп видны прямо здесь: {motif}. "
    )
    return (
        f"{head}Сегодня стая идёт {place['to']}. "
        f"{motif_line}{law_line}"
        + closings[rng.randrange(len(closings))]
    )


def _cards(rng: random.Random, day_index: int) -> list[CardDraft]:
    """Карты дня: тройка «заголовок—описание—последствие» выбирается целиком
    по соли запуска — перезапуски дают разные наборы вместо вечного цикла
    «день N = одни и те же карты». Пул на тег один, последствие всегда
    вытекает из своего действия; rng раскладывает только порядок карт.
    """

    def pick(paths: list[tuple[str, str, str]]) -> CardDraft:
        title, description, consequence = paths[rng.randrange(len(paths))]
        return (title, description, consequence)

    risk_title, risk_desc, risk_conseq = pick(_RISK_PATHS)
    care_title, care_desc, care_conseq = pick(_CARE_PATHS)
    cunning_title, cunning_desc, cunning_conseq = pick(_CUNNING_PATHS)
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


_OFFLINE_ECHO_OPENINGS = (
    "Утро несёт след вчерашней тропы: «{title}» уже впечатан в мир, и первая "
    "примета этого ждёт стаю прямо у развилки.",
    "Мир за ночь подтянулся под «{title}»: воздух чуть другой, тени ведут себя "
    "примеренно, а у карт лежит свежий отпечаток чужой лапы.",
    "«{title}» — так стая решила вчера, и утро начинается с подтверждения: "
    "дорога, которой ещё утром не было, уже протоптана.",
    "Рассвет проверил вчерашний выбор на прочность: «{title}» держит, и мир "
    "осторожно перестраивается вокруг этого решения.",
)


def offline_opening_echo(beat_title: str) -> str:
    """Детерминированная офлайн-строка открывающего эха (сеть молчит).

    Ставится перед готовой сценой заготовки тем же патчем, что и нейротекст.
    """
    rng = _rng(0, f"echo:{beat_title}")
    template = _OFFLINE_ECHO_OPENINGS[rng.randrange(len(_OFFLINE_ECHO_OPENINGS))]
    title = beat_title.strip() or "вчерашний путь"
    return template.format(title=title[:100])
