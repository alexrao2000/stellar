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


def test_analytical_page_shells_do_not_block_on_history(monkeypatch):
    monkeypatch.setattr(
        server,
        "_task_history",
        lambda: (_ for _ in ()).throw(AssertionError("page route loaded history")),
    )

    pages = [
        (server.page_calendar(), "/api/task_history"),
        (server.page_weight(), "/api/weights"),
        (server.page_wake(), "/api/wake"),
        (server.page_consistency(), "/api/consistency"),
        (server.page_data(), "/api/task_history"),
    ]

    for html, endpoint in pages:
        assert '"mode":"live"' in html
        assert endpoint in html


def test_measurement_apis_use_narrow_projections(monkeypatch):
    calls = []

    def projection(columns, *, title=None):
        calls.append((columns, title))
        if title == "Weigh yourself":
            return [{"day": "2026-08-19", "weight_lbs": "147.3"}]
        return [{"day": "2026-08-19", "notes": "Wake-up Time: 9:54 am"}]

    monkeypatch.setattr(server, "_task_projection", projection)

    assert server.api_weights() == [{"d": "2026-08-19", "w": 147.3}]
    assert server.api_wake() == [
        {"d": "2026-08-19", "time": "09:54:00", "w": 594}
    ]
    assert calls == [
        ("day,weight_lbs", "Weigh yourself"),
        ("day,notes", "Wake Up"),
    ]


def test_weight_warning_is_generous_but_catches_extreme_changes():
    previous = {"day": "2026-08-29", "weight_lbs": 150}

    assert server._weight_warning(170, previous) is None
    assert "20.1 lbs above" in server._weight_warning(170.1, previous)
    assert server._weight_warning(75, previous) is None
    assert "less than half" in server._weight_warning(74.9, previous)
    assert server._weight_warning(100, None) is None
