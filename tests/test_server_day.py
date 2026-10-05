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
    monkeypatch.setattr(server, "_definitions", lambda fresh=False: [])
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


def test_today_page_measurement_fields_are_their_own_task_tiles():
    """Weight and wake-up are one control each, not a field plus a task tile.

    The field carries the task's state -- solid while due, faded once filled --
    and the Tasks list no longer repeats them. The hint explaining the old
    two-control arrangement goes with it.
    """
    html = server.page_today("2026-08-30")

    assert 'id="field-weight"' in html
    assert 'id="field-wake"' in html
    assert "setTile('weight', r.is_completed ? 'done' : 'todo');" in html
    assert "setTile('wake', r.is_completed ? 'done' : 'todo');" in html
    assert ".task[data-field=" not in html
    assert "Saves as soon as you finish typing" not in html


def _defn(id_, title, sort_order=0, created_at="2026-08-01T00:00:00+00:00", active=True):
    return {"id": id_, "list_title": "Daily", "title": title, "sort_order": sort_order,
            "created_at": created_at, "active": active, "starts_on": None, "ends_on": None}


def test_default_order_is_measurements_first_then_creation():
    """With no order chosen, measurements lead; each group goes by creation."""
    defs = [
        _defn(1, "Brush Teeth", created_at="2026-08-01T00:00:00+00:00"),
        _defn(2, "Run", created_at="2026-07-01T00:00:00+00:00"),
        _defn(7, "Wake Up"),
        _defn(8, "Weigh yourself"),
    ]
    m = {"Weigh yourself", "Wake Up"}
    ordered = sorted(defs, key=lambda d: server._definitions_order_key(d, m))
    assert [d["title"] for d in ordered] == ["Wake Up", "Weigh yourself", "Run", "Brush Teeth"]


def test_a_chosen_order_overrides_the_default():
    defs = [_defn(7, "Wake Up", 3), _defn(8, "Weigh yourself", 1), _defn(1, "Run", 2)]
    m = {"Weigh yourself", "Wake Up"}
    ordered = sorted(defs, key=lambda d: server._definitions_order_key(d, m))
    assert [d["title"] for d in ordered] == ["Weigh yourself", "Run", "Wake Up"]


def test_day_read_lists_rows_in_definition_order(monkeypatch):
    defs = [_defn(1, "Run", 1), _defn(8, "Weigh yourself", 2), _defn(7, "Wake Up", 3)]
    rows = [{"id": i, "day": "2026-10-05", "list_title": "Daily", "title": t}
            for i, t in enumerate(["Wake Up", "Weigh yourself", "Run", "Retired habit"])]
    monkeypatch.setattr(server, "_definitions", lambda fresh=False: defs)
    monkeypatch.setattr(server, "_sb", lambda: object())
    monkeypatch.setattr(server, "_day_rows", lambda client, day: rows)

    result = server.api_day("2026-10-05", Response(), fresh=True)

    # A title with no definition (a habit that has since gone) goes last.
    assert [r["title"] for r in result] == ["Run", "Weigh yourself", "Wake Up", "Retired habit"]


class _OrderClient:
    def __init__(self):
        self.writes = []

    def table(self, name):
        assert name == "recurring_tasks"
        client = self

        class Q:
            def update(self, values):
                self.values = values
                return self

            def eq(self, column, value):
                assert column == "id"
                client.writes.append((value, self.values["sort_order"]))
                return self

            def execute(self):
                return SimpleNamespace(data=[])

        return Q()


def _order_setup(monkeypatch, defs):
    client = _OrderClient()
    monkeypatch.setattr(server, "_definitions", lambda fresh=False: defs)
    monkeypatch.setattr(server, "_sb", lambda: client)
    return client


def test_saving_an_order_writes_only_the_positions_that_changed(monkeypatch):
    defs = [_defn(7, "Wake Up", 1), _defn(8, "Weigh yourself", 2), _defn(1, "Run", 3)]
    client = _order_setup(monkeypatch, defs)

    result = server.api_task_order(server.TaskOrderBody(titles=["Weigh yourself", "Wake Up", "Run"]))

    assert result["order"] == ["Weigh yourself", "Wake Up", "Run"]
    assert sorted(client.writes) == [(7, 2), (8, 1)]      # Run stayed at 3: not written


def test_saving_a_partial_order_never_drops_a_task(monkeypatch):
    """Unknown titles are ignored; unsent active tasks keep their place after."""
    defs = [_defn(7, "Wake Up"), _defn(8, "Weigh yourself"), _defn(1, "Run"),
            _defn(9, "Old habit", active=False)]
    client = _order_setup(monkeypatch, defs)

    result = server.api_task_order(
        server.TaskOrderBody(titles=["Run", "Some retired title", "Old habit"]))

    assert result["order"] == ["Run", "Wake Up", "Weigh yourself"]
    assert sorted(client.writes) == [(1, 1), (7, 2), (8, 3)]


def test_saving_an_order_rejects_a_repeated_title(monkeypatch):
    import pytest
    from fastapi import HTTPException

    _order_setup(monkeypatch, [_defn(1, "Run")])
    with pytest.raises(HTTPException) as err:
        server.api_task_order(server.TaskOrderBody(titles=["Run", "Run"]))
    assert err.value.status_code == 400


def test_today_page_lists_measurements_and_tasks_in_one_reorderable_list():
    html = server.page_today("2026-08-30")

    assert "<h2>Measurements</h2>" not in html
    tasks = html.index('id="tasks"')
    assert html.index('id="item-weight"') > tasks
    assert html.index('id="item-wake"') > tasks
    assert "post('/api/task_order', { titles: ORDER })" in html
    assert "innerHTML = ''" not in html        # the list is never wiped wholesale
