from types import SimpleNamespace

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


def test_today_page_contains_accessible_weight_warning_tooltip():
    html = server.page_today("2026-08-30")

    assert 'id="weight-warning"' in html
    assert 'aria-describedby="weight-warning-tip"' in html
    assert 'id="weight-warning-tip"' in html
    assert 'role="tooltip"' in html


def test_today_page_accepts_tenth_of_a_pound_steps():
    html = server.page_today("2026-08-30")

    assert 'id="weight" type="number" inputmode="decimal" step="0.1"' in html


def test_today_page_saves_measurements_only_when_the_field_is_finished_with():
    """Never mid-typing.

    A debounce on `input` fires on any pause, so entering 146.8 posts "1",
    "14", "146" on the way; each accepted one re-renders and writes a formatted
    value into the box still under the cursor, which is where a stray ".0"
    came from. `change` on these inputs means blur or Enter.
    """
    html = server.page_today("2026-08-30")

    assert "el.addEventListener('change', save);" in html
    assert "el.addEventListener('input'" not in html
    assert "AUTOSAVE_DELAY_MS" not in html


def test_today_page_never_writes_into_a_field_being_typed_in():
    """A confirmation read or the background revalidation can land at any time.

    Both reach render(), which resets and then prefills the measurement
    fields. Either one touching a focused field looks exactly like the app
    eating your input.
    """
    html = server.page_today("2026-08-30")

    assert "if (document.activeElement === el) return;" in html
    assert "if (document.activeElement !== wEl) {" in html
    assert "if (w && document.activeElement !== kEl) {" in html


def test_today_page_queues_rapid_task_toggles():
    html = server.page_today("2026-08-30")

    assert "function syncDesired()" in html
    assert "if (!BOOT.writable || b.dataset.busy) return;" not in html
    assert "if (!b.dataset.syncing) syncDesired();" in html


def test_set_weight_materializes_today_before_saving(monkeypatch):
    row = {
        "id": 42,
        "day": "2026-08-30",
        "title": "Weigh yourself",
        "notes": "Current Weight: 150.0 lbs",
    }
    updated = {**row, "weight_lbs": 150.1, "is_completed": True}
    day_reads = iter([[], [row]])
    ensured = []

    class Query:
        def __init__(self):
            self.columns = ""

        def select(self, columns):
            self.columns = columns
            return self

        def eq(self, *_args):
            return self

        def lt(self, *_args):
            return self

        def filter(self, *_args):
            return self

        def order(self, *_args, **_kwargs):
            return self

        def limit(self, *_args):
            return self

        def update(self, *_args):
            self.columns = "update"
            return self

        def execute(self):
            return SimpleNamespace(data=[] if self.columns != "update" else [updated])

    class Client:
        def table(self, name):
            if name == "task_history":
                return Query()
            raise AssertionError(name)

    monkeypatch.setattr(server, "_check_writable", lambda day: day)
    monkeypatch.setattr(server, "_sb", lambda: Client())
    monkeypatch.setattr(server, "_day_rows", lambda _client, _day: next(day_reads))
    monkeypatch.setattr(
        server,
        "api_ensure_day",
        lambda day: ensured.append(day) or {"day": day, "created": ["Weigh yourself"]},
    )
    monkeypatch.setattr(server, "_cache_apply", lambda _rows: None)

    result = server.api_set_weight("2026-08-30", 150.1)

    assert ensured == ["2026-08-30"]
    assert result["weight_lbs"] == 150.1


def test_postgrest_failures_return_a_retryable_api_error():
    import asyncio
    from src.postgrest import PostgrestError

    response = asyncio.run(
        server.handle_postgrest_error(None, PostgrestError("database unavailable"))
    )

    assert response.status_code == 503
