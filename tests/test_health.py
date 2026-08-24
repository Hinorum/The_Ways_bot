"""/health: живость без лжи — degraded вместо «ok» без данных."""

from types import SimpleNamespace

from app import main as main_module


async def test_health_returns_snapshot_payload(monkeypatch) -> None:
    async def good_snapshot():
        return {"status": "ok", "last_tick_age": 1.5}

    monkeypatch.setattr("app.ops.snapshot", good_snapshot)
    response = await main_module.health(SimpleNamespace())
    assert response.status == 200
    assert b'"last_tick_age"' in response.body


async def test_health_stays_green_and_honest_when_snapshot_fails(monkeypatch) -> None:
    """Переходное окно (например, инвалидация планов после миграции) не должно
    ронять эндпоинт: Render видит живой процесс, а статус честно degraded."""

    async def broken_snapshot():
        raise RuntimeError("cached statement plan is invalid")

    monkeypatch.setattr("app.ops.snapshot", broken_snapshot)
    response = await main_module.health(SimpleNamespace())
    assert response.status == 200
    assert b'"degraded"' in response.body
    assert b'"ok"' not in response.body
