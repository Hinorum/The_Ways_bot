import io
from app.lore import compose_chapter

lines = []
def show(day, prev, season=None, salt="seedA"):
    ch = compose_chapter(day, prev, win_rule=None, season_block=season, salt=salt)
    lines.append(f"\n===== ДЕНЬ {day} =====")
    lines.append("TITLE: " + ch["title"])
    lines.append("PLACE: " + ch["place"])
    lines.append("TEXT: " + ch["text"])
    lines.append("CARDS:")
    for c in ch["cards"]:
        lines.append(f"  [{c['tag']}] {c['title']}")
        lines.append(f"     desc: {c['description']}")
        lines.append(f"     cons: {c['consequence']}")

show(1, [])
show(4, ["Кабель в зубах: Стая удержала кабель и дала фору.",
         "Общая будка: лагерь стал общим."])
show(14, ["Красный сигнал: источник ответил раньше срока.",
          "Чужое имя: архив поверил чужому имени.",
          "Тёплые миски: миски опустели к рассвету."])
show(60, ["Перехватить зов: стая стала зовом."], season="ДЕНЬ ПЕРВОГО ЛАЯ — финал сезона.")

with io.open("out.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("OK")
