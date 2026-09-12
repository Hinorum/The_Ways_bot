"""Лёгкий разделяемый статус стартового сидинга мира для /health.

Держится в памяти процесса и не тянет ни БД, ни тяжёлых модулей: его
импортируют и main (пишет), и ops.snapshot (читает для /health). Так /health
честно отвечает, идёт ли ещё фоновый сидинг AI-мира, завершился ли он, или
упал — и игра живёт на хардкод-фолбэках.

Значения world_seed_status:
    "pending"  — процесс поднялся, сидинг ещё не начинался;
    "seeding"  — фоновый сидинг идёт (десятки LLM-вызовов);
    "ready"    — сидинг завершился успешно;
    "fallback" — сидинг упал, работаем на хардкод-фолбэках.
"""
from __future__ import annotations

world_seed_status: str = "pending"


def set_world_seed_status(value: str) -> None:
    global world_seed_status
    world_seed_status = value


def get_world_seed_status() -> str:
    return world_seed_status
