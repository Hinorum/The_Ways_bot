"""Полный тестовый забег: 60 дней + все ветки."""
import io
from app.lore import compose_chapter, _echo
from app.season import (
    act_line, act_line_short, crisis_act, is_run_finale, midpoint_day,
    villain_stage, season_banner, season_block, current_season,
    finale_instruction, tag_balance_line, opener_instruction,
    run_position, _run_position_full, _crisis_window, FINALE_CARDS,
    _ACT_TONE, _MIDPOINT_BLOCK, _CULMINATION_BLOCK,
)
from app.prologue import prologue_block
from datetime import datetime, timedelta, timezone

lines = []
def w(s=""):
    lines.append(s)

# --- Симуляция забега 60 дней ---
anchor = {"dom": 1, "key": "2026-08", "season": 1, "order_axis": 1, "moral_axis": -1}
beat_titles = []
balance = {"risk": 0, "care": 0, "cunning": 0}
all_beats = []
moment = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)

w("=" * 60)
w("ПОЛНЫЙ АНАЛИТИЧЕСКИЙ ЗАБЕГ: ЭХО СТАИ — 60 ДНЕЙ")
w("=" * 60)

for day in range(1, 61):
    w(f"\n{'─' * 40}")
    moment = datetime(2026, 8, 1, 12, tzinfo=timezone.utc) + timedelta(days=day - 1)
    run_day, total = run_position(anchor, moment)
    season = current_season(anchor, moment)
    is_finale = is_run_finale(run_day, total)
    crisis = crisis_act(run_day, total)
    midpoint = midpoint_day(run_day, total)
    vs = villain_stage(run_day, total)
    
    w(f"ДЕНЬ {day} | season={season} run_day={run_day}/{total} act={'3-CRISIS' if crisis else '1' if run_day<=7 else '2'} villain_stage={vs} midpoint={midpoint}")
    
    # Сезонный блок
    sb = season_block(anchor=anchor, moment=moment, balance=balance)
    w(f"SEASON_BLOCK: {sb[:300]}...")
    
    # Пролог
    pb = prologue_block(run_day, alignment_label="хаотичная")
    if pb:
        w(f"PROLOGUE: {pb[:200]}...")
    
    # Предыдущие биты (берём последние 5)
    prev_beats = [f"{t}: последствие" for t in beat_titles[-5:]] if beat_titles else []
    
    # Генерируем главу
    ch = compose_chapter(
        day, prev_beats, win_rule=None,
        season_block=sb if not is_finale else "",
        salt="audit2026",
    )
    
    w(f"TITLE: {ch['title']}")
    w(f"PLACE: {ch['place']}")
    w(f"TEXT (первые 500 символов):")
    w(ch['text'][:500])
    w(f"CARDS ({len(ch['cards'])} шт):")
    for c in ch['cards']:
        tag_mark = "⚠️" if c['tag'] == 'care' and any(w in c['description'].lower() for w in ['разлом', 'прорыв', 'кабель', 'клыки', 'сигнал']) else "  "
        w(f"  {tag_mark}[{c['tag']}] «{c['title']}» — {c['consequence'][:120]}")
    
    # Запоминаем победителя (берём карту по умолчанию - care)
    winner = [c for c in ch['cards'] if c['tag'] == 'care']
    if winner:
        beat_titles.append(f"{winner[0]['title']}: {winner[0]['consequence'][:100]}")
        all_beats.append(winner[0])
        balance['care'] += 1
    
    # Финал
    if is_finale:
        fi = finale_instruction(balance)
        w(f"\nFINALE INSTRUCTION: {fi[:400]}...")
    
    # Баннер сезона
    banner = season_banner(anchor, moment)
    if banner:
        w(f"📢 SEASON BANNER: {banner}")

w("\n" + "=" * 60)
w("ИТОГОВАЯ СТАТИСТИКА")
w("=" * 60)
w(f"Всего дней: 60")
w(f"Баланс тегов: {balance}")
w(f"Всего карт в мире: {len(all_beats)}")
w(f"CARD TITLES (уникальные): {len(set(b['title'] for b in all_beats))}")
unique_titles = set(b['title'] for b in all_beats)
w(f"ПОЛНЫЙ СПИСОК ВЫБРАННЫХ КАРТ:")
for title in sorted(unique_titles):
    count = sum(1 for b in all_beats if b['title'] == title)
    w(f"  «{title}» — выбрана {count} раз(а)")

w("\n" + "=" * 60)
w("КРИТИЧЕСКАЯ ПРОВЕРКА: ДУБЛИ, СТИЛИСТИКА, СООТВЕТСТВИЕ")
w("=" * 60)

# Проверяем повторы описаний
descs = [b['description'] for b in all_beats]
for d in set(descs):
    if descs.count(d) > 1:
        w(f"⚠️ ДУБЛИРОВАНИЕ ОПИСАНИЯ ({descs.count(d)}x): «{d[:80]}...»")

# Проверяем повторы последствий
cons = [b['consequence'] for b in all_beats]
for c in set(cons):
    if cons.count(c) > 1:
        w(f"⚠️ ДУБЛИРОВАНИЕ ПОСЛЕДСТВИЯ ({cons.count(c)}x): «{c[:80]}...»")

# Стилистический разбор
w("\n" + "=" * 60)
w("СТИЛИСТИЧЕСКИЕ ЗАМЕЧАНИЯ ПО ГЛАВАМ")
w("=" * 60)

for day in [1, 2, 3, 4, 10, 14, 28, 30, 45, 54, 58, 59, 60]:
    if day > 60:
        continue
    moment_d = datetime(2026, 8, 1, 12, tzinfo=timezone.utc) + timedelta(days=day - 1)
    prev = [f"{beat_titles[i]}: описание" for i in range(max(0, day-4), day-1)] if day > 1 else []
    ch = compose_chapter(day, prev, win_rule=None, salt="audit2026")
    text = ch['text']
    
    w(f"\n--- ДЕНЬ {day}: «{ch['title']}» ---")
    w(f"Длина текста: {len(text)} символов")
    
    # Поиск проблем
    issues = []
    if "чужое эхо" in text.lower():
        issues.append("⚠️ F: ссылка на «чужое эхо» без прошлого")
    if text.count("portal") > 2 or text.count("портал") > 3:
        issues.append("⚠️ P: перенасыщение «порталами»")
    if text.count("стая") > 5:
        issues.append("⚠️ S: перенасыщение «стая»")
    if "  " in text:
        issues.append("⚠️ T: двойной пробел")
    if text and text[-1] not in ".!?…":
        issues.append("⚠️ P: текст без завершающего знака")
    if len(text) < 400:
        issues.append("⚠️ L: текст короче 400 символов")
    if len(text) > 1200:
        issues.append("⚠️ L: текст длиннее 1200 символов")
    
    # Проверка на противоречия в тоне
    if "тише" in text.lower() and "громче" in text.lower():
        issues.append("⚠️ B: КОНТРАДИКЦИЯ «тише» + «громче»")
    if "резче" in text.lower() and "тише" in text.lower():
        issues.append("⚠️ B: КОНТРАДИКЦИЯ «резче» + «тише»")
    
    for issue in issues:
        w(f"  {issue}")
    
    if not issues:
        w(f"  ✓ Без замечаний")

w("\n" + "=" * 60)
w("АУДИТ МЕНЮ / КОМАНД")
w("=" * 60)
w("""
/start — приветствие (пролог 5 собак)
/today — текущий день (глава + карты + голосование)
/lore — лор-дайджест (эхо-дайджест из прошлого)
/score — личный счёт игрока
/calling — текущий зов (призыв к голосованию)
/best — лучшие игроки
/wallet — привязка TON-кошелька
/stake — ставка TON
/top — лидерборд месяца
/change — смена голоса (ревот)
/advance — досрочное закрытие дня (админ)
/resetgame — полный сброс игры (админ)
/payouts — очередь выплат
/payout — ручной разбор выплаты
/treasury — состояние казны
/adjust — ручная корректировка
/pause — пауза бота
/resume — возобновление
""")
w("CALLBACKS:")
w("  vote — голосование")
w("  sniff — нюх (попытка учуять тропу)")
w("  remember — «Я помню этот след»")
w("  score:view — просмотр счёта")
w("  calling:pick — выбор зова")

with io.open("full_audit.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("DONE", len(lines), "lines")
