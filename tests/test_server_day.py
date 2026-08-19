from fastapi import Response

from src import server


def test_fresh_day_bypasses_process_history_cache(monkeypatch):
    row = {
        "id": 7,
        "day": "2026-08-19",
        "list_title": "Daily",
        "title": "Wake Up",
        "is_completed": True,
        "notes": "Wake-up Time: 7:15 am",
    }

    monkeypatch.setattr(server, "_sb", lambda: object())
    monkeypatch.setattr(server, "_day_rows", lambda client, day: [row])
    monkeypatch.setattr(server, "_sort_order", lambda: {"Wake Up": 1})
    monkeypatch.setattr(
        server,
        "_task_history",
        lambda: (_ for _ in ()).throw(AssertionError("fresh read used the history cache")),
    )

    response = Response()
    result = server.api_day("2026-08-19", response, fresh=True)

    assert result[0]["notes"] == "Wake-up Time: 7:15 am"
    assert result[0]["is_completed"] is True
    assert response.headers["cache-control"] == "no-store"


def test_ensure_day_uses_only_day_scoped_rows(monkeypatch):
    definitions = [
        {
            "list_title": "Daily",
            "title": "Wake Up",
            "starts_on": None,
            "ends_on": None,
        }
    ]
    existing = [{"list_title": "Daily", "title": "Wake Up"}]

    monkeypatch.setattr(server, "_recurring_defs", lambda: definitions)
    monkeypatch.setattr(server, "_sb", lambda: object())
    monkeypatch.setattr(server, "_day_rows", lambda client, day: existing)
    monkeypatch.setattr(
        server,
        "_task_history",
        lambda: (_ for _ in ()).throw(AssertionError("ensure loaded full history")),
    )

    result = server.api_ensure_day("2026-08-19")

    assert result == {
        "day": "2026-08-19",
        "created": [],
        "already_present": 1,
    }


def test_serverless_startup_does_not_warm_large_caches(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setattr(
        server,
        "_task_history",
        lambda: (_ for _ in ()).throw(AssertionError("warmed history on Vercel")),
    )
    monkeypatch.setattr(
        server,
        "_pool_all",
        lambda: (_ for _ in ()).throw(AssertionError("warmed pool on Vercel")),
    )

    server._warm_cache()
