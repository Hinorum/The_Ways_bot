"""Канон Пепельного Тракта: сцены собираются из прошлых Следов без платного API."""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, replace


WORLD_BIBLE = (
    "Мир называется Пепельный Тракт: сеть дорог между осколками городов, "
    "где память хранят не книги, а Следы — решения толпы, впечатанные в землю. "
    "Пёс-проводник без имени помнит все развилки. Колокол звонит, только если "
    "стая ошиблась в законе дня. Монета с двумя аверсами не даёт честного жребия."
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
        "дым": "risk",
        "ржав": "risk",
        "кост": "care",
        "стаи": "care",
        "монет": "cunning",
        "зеркал": "cunning",
        "колокол": "risk",
        "ключ": "risk",
        "шёпот": "care",
        "провод": "cunning",
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


def compose_chapter(day_index: int, previous_beats: list[str], win_rule=None, echoes=None) -> dict:
    rng = _rng(day_index, "|".join(previous_beats[-5:]))
    history_tags = tags_from_beats(previous_beats)
    last = previous_beats[-1] if previous_beats else None
    echo = _echo(last, history_tags)
    places = [
        {
            "where": "на рынке без продавцов",
            "to": "к рынку без продавцов",
            "by": "у рынка без продавцов",
            "round": "рынок без продавцов",
        },
        {
            "where": "на мосту из слежавшейся золы",
            "to": "к мосту из слежавшейся золы",
            "by": "у моста из слежавшейся золы",
            "round": "мост из слежавшейся золы",
        },
        {
            "where": "на станции, где поезда ходят только назад",
            "to": "к станции, где поезда ходят только назад",
            "by": "у станции, где поезда ходят только назад",
            "round": "станцию, где поезда ходят только назад",
        },
        {
            "where": "у колодца, в котором слышно вчерашнее голосование",
            "to": "к колодцу, в котором слышно вчерашнее голосование",
            "by": "у колодца, в котором слышно вчерашнее голосование",
            "round": "колодец, в котором слышно вчерашнее голосование",
        },
        {
            "where": "в роще фонарей, горящих чужим теплом",
            "to": "к роще фонарей, горящих чужим теплом",
            "by": "у рощи фонарей, горящих чужим теплом",
            "round": "рощу фонарей, горящих чужим теплом",
        },
        {
            "where": "в казарме молчаливых проводников",
            "to": "к казарме молчаливых проводников",
            "by": "у казармы молчаливых проводников",
            "round": "казарму молчаливых проводников",
        },
        {
            "where": "на ярмарке потерянных имён",
            "to": "к ярмарке потерянных имён",
            "by": "у ярмарки потерянных имён",
            "round": "ярмарку потерянных имён",
        },
        {
            "where": "на берегу реки, текущей против памяти",
            "to": "к берегу реки, текущей против памяти",
            "by": "у берега реки, текущей против памяти",
            "round": "берег реки, текущей против памяти",
        },
    ]
    place = places[(day_index + len(history_tags)) % len(places)]
    cover_scenes = [
        "abandoned market with empty stalls under falling ash",
        "bridge of compacted grey ash spanning a fog abyss",
        "railway station where trains run backwards, reversed rails",
        "old stone well whispering with yesterday's voices",
        "grove of lanterns burning with borrowed warmth",
        "barracks of silent guides, sleeping hounds in rows",
        "fairground of lost names, faceless masks on strings",
        "river shore where water flows against memory",
    ]
    scene = cover_scenes[(day_index + len(history_tags)) % len(cover_scenes)]
    cover_prompt = (
        "dark fantasy matte painting, wide establishing shot, " + scene +
        ", lone nameless dog guide silhouette, distant cracked bell tower, "
        "drifting embers, muted ashen palette, cinematic light, no text, no letters"
    )
    title_bits = [
        "Трое ворот",
        "Пепел на языке",
        "Закон под языком",
        "Стая считает тишину",
        "Монета не падает",
        "Колокол держит паузу",
        "След теплее крови",
        "Дорога выбирает нас",
    ]
    cards = _cards(rng, place)
    active_echoes = list(echoes or [])
    text = _chapter_text(day_index, echo, place, history_tags, last, win_rule)
    if active_echoes:
        touches = (
            "У края дороги снова мелькает «{name}».",
            "Где-то за спиной остаётся «{name}».",
            "«{name}» не отпускает тропу.",
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
            if sentence and sentence[-1] not in ".!?…":
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
        return "Путники впервые стоят у края Тракта. Ещё ни один След не впечатан."
    dominant = max(set(tags), key=tags.count) if tags else "care"
    tone = {
        "risk": "Мир стал резче: дым и ржавчина держатся в шерсти пса.",
        "care": "Мир стал тише: стая помнит имена и греет След телами.",
        "cunning": "Мир стал кривым: зеркала и монеты путают вчера с сегодня.",
    }[dominant]
    title, _, deed = last.partition(":")
    title = title.strip() or "вчерашний путь"
    deed = deed.strip()
    if len(deed) > 180:
        deed = deed[:177] + "…"
    if deed and deed[-1] not in ".!?…":
        deed += "."
    if deed:
        return f"{tone} Вчера стая выбрала «{title}» — {deed} Мир запомнил."
    return f"{tone} Вчера стая выбрала «{title}», и Тракт запомнил этот шаг."


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

        law_line = f"Колокол объявил закон дня: {RULE_PHRASES[win_rule]}. "
    if day_index == 1:
        text = (
            "Ты стоишь у края Пепельного Тракта. Пёс-проводник садится в пыли "
            "и смотрит на трое ворот: ржавые, костяные и зеркальные. "
            "За каждым — первый След. "
        )
        if win_rule is not None:
            from app.models import RULE_PHRASES

            text += (
                f"Колокол объявляет закон первого дня: {RULE_PHRASES[win_rule]}. "
                "Счёт скрыт до итогов, так что выбирай сердцем. "
            )
        else:
            text += "Закон первого дня объявят утром. "
        return text + "Выбери ворота. Завтра земля запомнит только одно."
    motif = "колокол молчит"
    if tags.count("risk") >= tags.count("care"):
        motif = "колокол дрожит, но ещё не бьёт"
    if tags.count("cunning") >= 2:
        motif = "монета с двумя аверсами крутится в пыли и не падает"
    yesterday = last.split(":")[0].strip() if last else "первый день"
    closings = [
        (
            "Три пути снова равны по цене. Счёт скрыт до итогов, но закон известен с утра. "
            "Кто бы ни победил после суток тишины — это станет новым каноном, "
            "и следующие дни будут ссылаться на этот поворот, как на кость в горле мира."
        ),
        (
            "Опять три дороги и один закат. Цифры спрятаны до итогов, закон объявлён с утра. "
            "Что бы ни выбрала стая, земля впечатает это в канон — и завтра псу придётся "
            "идти по новому."
        ),
        (
            "Развилка ждёт ровно сутки. Закон известен, счёт скрыт. Победа стаи станет "
            "каноном раньше, чем остынет пепел на подошвах."
        ),
        (
            "Пёс садится у развилки и ждёт решения стаи: он пойдёт куда скажут, но помнить "
            "будет дольше всех. Сутки на выбор; закон дня объявлен с самого утра."
        ),
        (
            "Дальше дорога сужается до трёх троп сразу — такое бывает только здесь. "
            "Выбор у стаи один на всех и сутки на раздумья; земля уже приготовилась "
            "впечатать итог."
        ),
    ]
    return (
        f"{echo} Сегодня Тракт выводит стаю {place['to']}. "
        f"Пёс обнюхивает воздух и тычется носом в след «{yesterday}», будто проверяет, "
        f"не солгала ли земля. {law_line}{motif.capitalize()}. "
        + closings[rng.randrange(len(closings))]
    )


_RISK_TITLES = [
    "Ржавый проход",
    "Дымовая тропа",
    "Шаг под колокол",
    "Железный переулок",
    "Голодный свист",
]
_CARE_TITLES = [
    "Костёр стаи",
    "Костяной приют",
    "Имена в шерсти",
    "Сон у костра",
    "Тёплый подпал",
]
_CUNNING_TITLES = [
    "Двойной аверс",
    "Зеркальный объезд",
    "Сделка с проводником",
    "Кривая монета",
    "Дверь со спины",
]

_RISK_DESCRIPTIONS = [
    "Рискнуть {where}: идти туда, где пахнет железом и чужой ошибкой.",
    "Рискнуть {where}: свернуть с тропы туда, где даже пёс прижимает уши.",
    "Рискнуть {where}: постучать в то, что заперто изнутри.",
    "Рискнуть {where}: пойти на запах гари раньше, чем колокол передумает.",
]
_CARE_DESCRIPTIONS = [
    "Остаться {by} и греть вчерашний След, пока он не остыл.",
    "Остаться {by} и пересчитать стаю по именам, которых нет.",
    "Остаться {by} и дать ночлег тому, кто придёт без лица.",
    "Остаться {by} и дослушать пса: он начинает с середины, но никогда не лжёт.",
]
_CUNNING_DESCRIPTIONS = [
    "Обойти {round} хитростью: отдать невозможный жребий встречному проводнику.",
    "Обойти {round} хитростью: поменять местами вчерашнее и завтрашнее.",
    "Обойти {round} хитростью: солгать зеркалам правду.",
    "Обойти {round} хитростью: заплатить за всех и забыть, кем были.",
]

_RISK_CONSEQUENCES = [
    "Стая уносит запах гари. В каноне появляется ржавый ключ: он открывает только надломленное.",
    "В земле остаётся выжженный след подковы, смотрящий против ветра; с той стороны Тракта теперь слышно железо.",
    "Канон принимает дым: отныне у перекрёстков стоит тот, кто не представился.",
    "У переправы заводится паром из костей; плату он берёт вперёд — чьим-то именем. Канон запоминает цену.",
    "Ночью Тракт меняет обочины местами. В каноне отмечена тропа, которой вчера не было.",
    "Стая слышит голодный свист за поворотом. Канон теперь знает: часть дороги считает себя хищником.",
]
_CARE_CONSEQUENCES = [
    "Появляется общий костёр проигравших дней. Пёс носит в шерсти имена тех, кто не угадал закон.",
    "Стая запоминает тепло: в каноне появляется место, где Следы не остывают до утра.",
    "Чужак оставляет у костра монету на два аверса. Канон отмечает: этот долг вернут памятью.",
    "У стаи заводится маленький колокол — для тех, кто ошибся один раз. Его звон заметно мягче.",
    "Пёс получает кличку, которую никто не решился произнести вслух. Канон хранит её между строк.",
    "Сломанное вчера срастается криво, но крепко. В каноне отмечен шов, что прочнее целого.",
]
_CUNNING_CONSEQUENCES = [
    "Монета с двумя аверсами уходит в чужой карман. В зеркалах спорит второй Тракт.",
    "Канон кривится: с этого дня одна из дорог ведёт туда же, но позже.",
    "Проводник берёт жребий и не отдаёт сдачу. В каноне появляется счёт, который сведут завтра.",
    "Колокол соглашается на рассрочку: теперь он звонит с опозданием в один день, и канон обязан это помнить.",
    "Одна из дорог устала от прямых ответов и начала петлять. На карте канона завязался узел.",
    "Чужие долги оседают на шерсти стаи. Канон предупреждает мимоходом: их примут за свои.",
]


def _cards(rng: random.Random, place: dict) -> list[CardDraft]:
    risk = CardDraft(
        title=rng.choice(_RISK_TITLES),
        description=rng.choice(_RISK_DESCRIPTIONS).format(**place),
        consequence=rng.choice(_RISK_CONSEQUENCES),
        tag="risk",
        image_prompt=(
            "dark fantasy tarot card, rusted gates on an ashen road, silent nameless dog, "
            "embers, cinematic lighting, no text, no letters, oil painting"
        ),
    )
    care = CardDraft(
        title=rng.choice(_CARE_TITLES),
        description=rng.choice(_CARE_DESCRIPTIONS).format(**place),
        consequence=rng.choice(_CARE_CONSEQUENCES),
        tag="care",
        image_prompt=(
            "dark fantasy tarot card, pack of travelers around a small fire, bone shrine, "
            "gentle dog guide, ash snow, cinematic, no text, no letters, oil painting"
        ),
    )
    cunning = CardDraft(
        title=rng.choice(_CUNNING_TITLES),
        description=rng.choice(_CUNNING_DESCRIPTIONS).format(**place),
        consequence=rng.choice(_CUNNING_CONSEQUENCES),
        tag="cunning",
        image_prompt=(
            "dark fantasy tarot card, two-headed coin hovering, mirror road, trickster lantern, "
            "silent dog in reflection, cinematic, no text, no letters, oil painting"
        ),
    )
    cards = [risk, care, cunning]
    rng.shuffle(cards)
    return cards
