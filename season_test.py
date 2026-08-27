import io
from datetime import date, datetime, timezone

from app import config
from app.season import _run_position_full, run_position, current_season
from app.season import (
    _run_position_full,
    run_position,
    current_season,
    crisis_act,
    season_banner,
    act_line,
)

config.settings.first_season_months = 1
config.settings.run_length_months = 2

anchor = {"dom": 1, "key": "2026-01", "season": 1}
lines = []


def check(name, got, exp):
    ok = got == exp
    lines.append(f"{'OK ' if ok else 'FAIL'} {name}: got={got} exp={exp}")


# --- Эпохи: сезон 1 = 1 месяц (31 день, янв), сезон 2+ = 2 месяца ---
check("s1 day1", _run_position_full(anchor, datetime(2026, 1, 1, tzinfo=timezone.utc)), (1, 31, 1))
check("s1 last", _run_position_full(anchor, datetime(2026, 1, 31, tzinfo=timezone.utc)), (31, 31, 1))
# 1 февраля — первый день сезона 2 (февраль 2026 — 28 дней, итого 28+31=59)
rd, tot, s = _run_position_full(anchor, datetime(2026, 2, 1, tzinfo=timezone.utc))
check("s2 day1 run_day", rd, 1)
check("s2 total", tot, 59)
check("s2 number", s, 2)
# 1 апреля — первый день сезона 3
rd3, tot3, s3 = _run_position_full(anchor, datetime(2026, 4, 1, tzinfo=timezone.utc))
check("s3 day1 run_day", rd3, 1)
check("s3 number", s3, 3)

# --- run_position / current_season совместимы ---
check("run_position shape", run_position(anchor, datetime(2026, 2, 1, tzinfo=timezone.utc))[0], 1)
check("current_season", current_season(anchor, datetime(2026, 2, 1, tzinfo=timezone.utc)), 2)

# --- Анонс нового сезона только в день 1 сезона >1 ---
check("banner s2", bool(season_banner(anchor, datetime(2026, 2, 1, tzinfo=timezone.utc))), True)
check("banner s1", season_banner(anchor, datetime(2026, 1, 5, tzinfo=timezone.utc)), None)

# --- Масштаб кризиса: короткий сезон 7 дней, длинный — больше ---
# сезон1 = 31 день, окно кризиса 7 -> последние 7 дней (25..31)
check("crisis s1 non-crisis", crisis_act(24, 31), False)    # 31-24=7, не <7
check("crisis s1 crisis", crisis_act(25, 31), True)         # 31-25=6 <7
# сезон2 = 59 дней, окно кризиса max(7, 59//5=11)=11 -> последние 11 дней (49..59)
check("crisis s2 non-crisis", crisis_act(40, 59), False)    # 59-40=19
check("crisis s2 crisis", crisis_act(50, 59), True)         # 59-50=9 <11

# --- act_line показывает номер сезона ---
al = act_line(1, 31, season=1)
check("act_line season tag", al.startswith("Сезон 1. "), True)

with io.open("season_out.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("DONE")
