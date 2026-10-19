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


def test_set_weight_makes_only_its_own_row(db, monkeypatch):
    """Recording a weight on a day with no rows makes that one row, not the day."""
    fake = db([_task(8, "Weigh yourself", sort_order=1), _task(1, "Run", sort_order=2)])
    monkeypatch.setattr(server, "_day_rows", lambda client, day: [
        r for r in fake.tables["task_history"] if r["day"] == day])
    server.api_set_weight("2026-10-13", 150.2)
    rows = fake.tables["task_history"]
    assert [(r["title"], r["is_completed"]) for r in rows] == [("Weigh yourself", True)]

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
    # The list is never wiped wholesale: the measurement entries live in it.
    assert "host.innerHTML = ''" not in html
    assert "getElementById('tasks').innerHTML" not in html


# ---- optional tasks and deadlines ------------------------------------------
def test_default_order_puts_optional_tasks_last():
    defs = [
        _defn(1, "Run", created_at="2026-07-01T00:00:00+00:00") | {"optional": True},
        _defn(2, "Floss", created_at="2026-08-01T00:00:00+00:00"),
        _defn(8, "Weigh yourself"),
    ]
    m = {"Weigh yourself", "Wake Up"}
    ordered = sorted(defs, key=lambda d: server._definitions_order_key(d, m))
    assert [d["title"] for d in ordered] == ["Weigh yourself", "Floss", "Run"]


def test_deadline_reaches_into_the_next_day():
    from zoneinfo import ZoneInfo
    la = ZoneInfo("America/Los_Angeles")
    # 23:00 on Oct 12 Pacific (PDT, UTC-7) and 01:30 on Oct 13 via 1530 minutes.
    assert server._deadline_at("2026-10-12", 23 * 60, la) == "2026-10-13T06:00:00+00:00"
    assert server._deadline_at("2026-10-12", 1440 + 90, la) == "2026-10-13T08:30:00+00:00"
    assert server._deadline_at("2026-10-12", None, la) is None


def test_deadline_keeps_its_clock_time_across_a_daylight_saving_change():
    from zoneinfo import ZoneInfo
    la = ZoneInfo("America/Los_Angeles")
    # Clocks go back at 2am on 2026-11-01; 1:00am the next day is still PST 01:00.
    assert server._deadline_at("2026-10-31", 1440 + 60 + 60, la) == "2026-11-01T10:00:00+00:00"


def test_day_read_carries_optional_and_deadline(monkeypatch):
    defs = [_defn(1, "Run", 1) | {"optional": True, "deadline_minutes": 1530},
            _defn(2, "Floss", 2) | {"optional": False, "deadline_minutes": None}]
    rows = [{"id": 1, "day": "2026-10-12", "list_title": "Daily", "title": "Run"},
            {"id": 2, "day": "2026-10-12", "list_title": "Daily", "title": "Floss"}]
    monkeypatch.setattr(server, "_definitions", lambda fresh=False: defs)
    monkeypatch.setattr(server, "_sb", lambda: object())
    monkeypatch.setattr(server, "_day_rows", lambda client, day: rows)
    monkeypatch.setattr(server, "_shape", lambda r: dict(r))
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(server, "_app_tz", lambda: ZoneInfo("America/Los_Angeles"))

    result = {r["title"]: r for r in server.api_day("2026-10-12", Response(), fresh=True)}

    assert result["Run"]["optional"] is True
    assert result["Run"]["deadline_at"] == "2026-10-13T08:30:00+00:00"
    assert result["Floss"]["optional"] is False and result["Floss"]["deadline_at"] is None
    assert "optional" not in rows[0]          # the cached rows are not mutated


def _completion_setup(monkeypatch, row):
    from zoneinfo import ZoneInfo
    writes = []

    class Q:
        def select(self, cols): return self
        def update(self, values): writes.append(values); return self
        def eq(self, column, value): return self
        def execute(self): return SimpleNamespace(data=[{**row, **(writes[-1] if writes else {})}])

    monkeypatch.setattr(server, "_sb", lambda: SimpleNamespace(table=lambda name: Q()))
    monkeypatch.setattr(server, "_app_tz", lambda: ZoneInfo("America/Los_Angeles"))
    monkeypatch.setattr(server, "_cache_apply", lambda rows: None)
    monkeypatch.setattr(server, "_check_writable", lambda day: day)
    return writes


def test_completion_time_can_be_corrected(monkeypatch):
    writes = _completion_setup(monkeypatch, {"day": "2026-10-11", "is_completed": True})
    server.api_set_completed_at(5, "2026-10-11T22:15:00-07:00")
    assert writes == [{"completed_at": "2026-10-12T05:15:00+00:00"}]


def test_completion_time_cannot_precede_the_day_or_be_unticked(monkeypatch):
    import pytest
    from fastapi import HTTPException

    _completion_setup(monkeypatch, {"day": "2026-10-11", "is_completed": True})
    with pytest.raises(HTTPException):
        server.api_set_completed_at(5, "2026-10-10T23:00:00-07:00")
    _completion_setup(monkeypatch, {"day": "2026-10-11", "is_completed": False})
    with pytest.raises(HTTPException):
        server.api_set_completed_at(5, "2026-10-11T22:15:00-07:00")


def test_consistency_marks_optional_rows(monkeypatch):
    monkeypatch.setattr(server, "_definitions",
                        lambda fresh=False: [_defn(1, "Run") | {"optional": True}, _defn(2, "Floss")])
    monkeypatch.setattr(server, "_task_projection", lambda cols, title=None: [
        {"day": "2026-10-11", "list_title": "Daily", "title": "Run", "is_completed": False},
        {"day": "2026-10-11", "list_title": "Daily", "title": "Floss", "is_completed": False}])
    rows = {r["title"]: r for r in server.api_consistency()}
    assert rows["Run"]["optional"] is True and rows["Floss"]["optional"] is False


# ---- task management: versions, add, stop, bin ------------------------------
import copy
import itertools

import pytest
from fastapi import HTTPException


class FakeDB:
    """Just enough PostgREST for recurring_tasks and task_history."""

    def __init__(self, defs, rows=()):
        self.tables = {"recurring_tasks": [dict(d) for d in defs],
                       "task_history": [dict(r) for r in rows]}
        self.ids = itertools.count(1000)

    def table(self, name):
        return _FakeQuery(self, name)


class _FakeQuery:
    def __init__(self, db, name):
        self.db, self.name, self.op, self.payload, self.preds = db, name, "select", None, []
        self.cap = None

    def select(self, *_a, **_k): return self
    def insert(self, payload): self.op, self.payload = "insert", payload; return self
    def update(self, payload): self.op, self.payload = "update", payload; return self
    def delete(self): self.op = "delete"; return self
    def limit(self, n): self.cap = n; return self

    def order(self, col, desc=False):
        self.sort = (col, desc); return self

    def eq(self, col, val):
        self.preds.append(lambda r: r.get(col) == val); return self

    def lt(self, col, val):
        self.preds.append(lambda r: str(r.get(col)) < str(val)); return self

    def filter(self, col, op, val):
        ops = {"gte": lambda a, b: a >= b, "gt": lambda a, b: a > b,
               "lte": lambda a, b: a <= b, "lt": lambda a, b: a < b}
        self.preds.append(lambda r: ops[op](str(r.get(col)), str(val))); return self

    def execute(self):
        rows = self.db.tables[self.name]
        hit = [r for r in rows if all(p(r) for p in self.preds)]
        if self.op == "select":
            data = [dict(r) for r in hit]
            if getattr(self, "sort", None):
                col, desc = self.sort
                data.sort(key=lambda r: str(r.get(col)), reverse=desc)
        elif self.op == "insert":
            new = dict(self.payload, id=next(self.db.ids))
            new.setdefault("series_id", None); new.setdefault("deleted_at", None)
            new.setdefault("ends_on", None)
            rows.append(new); data = [dict(new)]
        elif self.op == "update":
            for r in hit: r.update(self.payload)
            data = [dict(r) for r in hit]
        else:
            self.db.tables[self.name] = [r for r in rows if r not in hit]
            data = [dict(r) for r in hit]
        return SimpleNamespace(data=data[: self.cap] if self.cap else data)


def _task(id_, title, **kw):
    d = _defn(id_, title)
    d.update({"optional": False, "deadline_minutes": None, "series_id": None,
              "deleted_at": None, "starts_on": "2026-08-01", "ends_on": None})
    d.update(kw)
    return d


@pytest.fixture
def db(monkeypatch):
    holder = {}

    def make(defs, rows=()):
        fake = FakeDB(defs, rows)
        holder["db"] = fake
        monkeypatch.setattr(server, "_sb", lambda: fake)
        monkeypatch.setattr(server, "_definitions",
                            lambda fresh=False: copy.deepcopy(fake.tables["recurring_tasks"]))
        monkeypatch.setattr(server, "_today", lambda: "2026-10-13")
        monkeypatch.setattr(server, "_cache_apply", lambda rows: None)
        return fake

    return make


def _versions(fake, sid):
    return sorted((d for d in fake.tables["recurring_tasks"] if (d["series_id"] or d["id"]) == sid),
                  key=lambda d: d["starts_on"])


def test_optional_from_today_keeps_past_days_required(db):
    fake = db([_task(1, "Run", sort_order=1), _task(2, "Floss", sort_order=2)])
    server.api_change_task(1, server.TaskChange(optional=True))

    old, new = _versions(fake, 1)
    assert (old["ends_on"], old["optional"]) == ("2026-10-12", False)
    assert (new["starts_on"], new["ends_on"], new["optional"]) == ("2026-10-13", None, True)
    assert new["sort_order"] == 1 and new["series_id"] == 1
    assert server._version_on(_versions(fake, 1), "2026-09-01")["optional"] is False
    assert server._version_on(_versions(fake, 1), "2024-01-01")["optional"] is False
    assert server._version_on(_versions(fake, 1), "2026-10-20")["optional"] is True


def test_changing_twice_in_one_day_edits_the_same_version(db):
    fake = db([_task(1, "Run", sort_order=1)])
    server.api_change_task(1, server.TaskChange(optional=True))
    server.api_change_task(1, server.TaskChange(deadline_minutes=1380))
    assert len(_versions(fake, 1)) == 2
    assert _versions(fake, 1)[-1]["deadline_minutes"] == 1380


def test_all_days_rewrites_every_version(db):
    fake = db([_task(1, "Run", sort_order=1)])
    server.api_change_task(1, server.TaskChange(optional=True))
    server.api_change_task(1, server.TaskChange(deadline_minutes=600, all_days=True))
    assert [v["deadline_minutes"] for v in _versions(fake, 1)] == [600, 600]


def test_making_optional_on_the_default_order_pins_positions_first(db):
    fake = db([_task(8, "Weigh yourself"), _task(1, "Run"), _task(2, "Floss",
               created_at="2026-09-01T00:00:00+00:00")])
    server.api_change_task(1, server.TaskChange(optional=True))
    pos = {d["title"]: d["sort_order"] for d in fake.tables["recurring_tasks"]}
    assert pos == {"Weigh yourself": 1, "Run": 2, "Floss": 3}


def test_rename_from_a_day_renames_rows_from_that_day_only(db):
    rows = [{"id": 1, "day": "2026-10-12", "list_title": "Daily", "title": "Run", "is_completed": True},
            {"id": 2, "day": "2026-10-13", "list_title": "Daily", "title": "Run", "is_completed": False}]
    fake = db([_task(1, "Run", sort_order=1)], rows)
    server.api_change_task(1, server.TaskChange(title="Jog"))
    titles = {r["day"]: r["title"] for r in fake.tables["task_history"]}
    assert titles == {"2026-10-12": "Run", "2026-10-13": "Jog"}
    assert [v["title"] for v in _versions(fake, 1)] == ["Run", "Jog"]


def test_rename_cannot_apply_to_past_days_or_clash(db):
    db([_task(1, "Run", sort_order=1), _task(2, "Floss", sort_order=2)])
    with pytest.raises(HTTPException) as e:
        server.api_change_task(1, server.TaskChange(title="Jog", all_days=True))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        server.api_change_task(1, server.TaskChange(title="floss"))
    assert e.value.status_code == 409


def test_measurements_cannot_be_renamed_or_binned(db):
    db([_task(8, "Weigh yourself", sort_order=1)])
    with pytest.raises(HTTPException):
        server.api_change_task(8, server.TaskChange(title="Weight"))
    with pytest.raises(HTTPException):
        server.api_bin_task(8)


def test_a_change_cannot_start_below_the_write_floor(db):
    db([_task(1, "Run", sort_order=1)])
    with pytest.raises(HTTPException) as e:
        server.api_change_task(1, server.TaskChange(optional=True, from_day="2026-07-01"))
    assert e.value.status_code == 403


def test_added_task_goes_last_and_starts_today(db):
    fake = db([_task(1, "Run", sort_order=1), _task(2, "Floss", sort_order=2)])
    out = server.api_add_task(server.NewTask(title="  Stretch  ", optional=True))
    added = next(d for d in fake.tables["recurring_tasks"] if d["title"] == "Stretch")
    assert (added["sort_order"], added["starts_on"], added["optional"]) == (3, "2026-10-13", True)
    assert out["title"] == "Stretch"
    with pytest.raises(HTTPException):
        server.api_add_task(server.NewTask(title="run"))


def test_stop_ends_the_task_and_removes_blank_rows_after(db):
    rows = [{"id": 1, "day": "2026-10-13", "list_title": "Daily", "title": "Run", "is_completed": True},
            {"id": 2, "day": "2026-10-14", "list_title": "Daily", "title": "Run", "is_completed": False}]
    fake = db([_task(1, "Run", sort_order=1)], rows)
    out = server.api_stop_task(1, server.StopTask(last_day="2026-10-13"))
    assert out["ends_on"] == "2026-10-13" and out["blank_rows_removed"] == 1
    assert [r["id"] for r in fake.tables["task_history"]] == [1]


def test_stop_refuses_to_erase_recorded_days(db):
    rows = [{"id": 2, "day": "2026-10-14", "list_title": "Daily", "title": "Run", "is_completed": True}]
    fake = db([_task(1, "Run", sort_order=1)], rows)
    with pytest.raises(HTTPException) as e:
        server.api_stop_task(1, server.StopTask(last_day="2026-10-13"))
    assert e.value.status_code == 409
    assert fake.tables["recurring_tasks"][0]["ends_on"] is None


def test_bin_hides_and_restore_brings_back(db, monkeypatch):
    fake = db([_task(1, "Run", sort_order=1), _task(2, "Floss", sort_order=2)])
    server.api_bin_task(1)
    defs = fake.tables["recurring_tasks"]
    assert server._binned_titles(defs) == {"Run"}
    assert [c["title"] for _s, _v, c in server._current_series(defs)] == ["Floss"]
    server.api_restore_task(1)
    assert server._binned_titles(fake.tables["recurring_tasks"]) == set()


def test_permanent_delete_only_from_the_bin_and_only_without_history(db):
    rows = [{"id": 1, "day": "2026-10-13", "list_title": "Daily", "title": "Run", "is_completed": True},
            {"id": 2, "day": "2026-10-13", "list_title": "Daily", "title": "Typo", "is_completed": False}]
    fake = db([_task(1, "Run", sort_order=1), _task(2, "Typo", sort_order=2, starts_on="2026-10-13")], rows)
    with pytest.raises(HTTPException):
        server.api_delete_task(2)                     # not in the bin yet
    server.api_bin_task(1); server.api_bin_task(2)
    with pytest.raises(HTTPException):
        server.api_delete_task(1)                     # has a completion
    out = server.api_delete_task(2)
    assert out["blank_rows_removed"] == 1
    assert {d["title"] for d in fake.tables["recurring_tasks"]} == {"Run"}
    assert [r["title"] for r in fake.tables["task_history"]] == ["Run"]


def test_day_read_judges_each_day_by_its_own_version_and_hides_the_bin(db, monkeypatch):
    defs = [_task(1, "Run", sort_order=1, ends_on="2026-10-12"),
            _task(5, "Run", sort_order=1, series_id=1, starts_on="2026-10-13", optional=True),
            _task(2, "Typo", sort_order=2, deleted_at="2026-10-13T00:00:00Z")]
    fake = db(defs)
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(server, "_app_tz", lambda: ZoneInfo("America/Los_Angeles"))
    monkeypatch.setattr(server, "_shape", lambda r: dict(r))
    for day, optional in (("2026-10-12", False), ("2026-10-13", True)):
        rows = [{"id": 1, "day": day, "list_title": "Daily", "title": "Run"},
                {"id": 2, "day": day, "list_title": "Daily", "title": "Typo"}]
        monkeypatch.setattr(server, "_day_rows", lambda client, d, rows=rows: rows)
        out = server.api_day(day, Response(), fresh=True)
        assert [(r["title"], r["optional"]) for r in out] == [("Run", optional)]


def test_calendar_gets_optional_as_day_ranges(db):
    db([_task(1, "Run", sort_order=1, ends_on="2026-10-12"),
        _task(5, "Run", sort_order=1, series_id=1, starts_on="2026-10-13", optional=True)])
    assert server._calendar_versions()["optional"] == {"Run": [["2026-10-13", None]]}


def test_settings_page_lists_tasks_and_flags_the_migration(db, monkeypatch):
    db([_task(1, "Run", sort_order=1), _task(2, "Typo", deleted_at="2026-10-13T00:00:00Z")])
    html = server.page_tasks()
    assert '"migrated":true' in html and '"title":"Run"' in html and '"binned":true' in html
    monkeypatch.setattr(server, "_definitions", lambda fresh=False: [_defn(1, "Run")])
    assert '"migrated":false' in server.page_tasks()


# ---- the Tasks tab: viewing never writes ---------------------------------------
def test_calendar_redirects_into_the_tasks_tab():
    r = server.page_calendar()
    assert r.status_code == 308 and r.headers["location"] == "/?view=week"


def test_a_day_lists_scheduled_tasks_without_writing(db, monkeypatch):
    from zoneinfo import ZoneInfo
    fake = db([_task(1, "Run", sort_order=1), _task(2, "Floss", sort_order=2, starts_on="2026-10-20")])
    monkeypatch.setattr(server, "_app_tz", lambda: ZoneInfo("America/Los_Angeles"))
    monkeypatch.setattr(server, "_day_rows", lambda client, day: [])
    out = server.api_day("2026-10-15", Response(), fresh=True)
    assert [(r["title"], r["id"], r["virtual"]) for r in out] == [("Run", None, True)]
    assert fake.tables["task_history"] == []          # nothing was written


def test_range_reads_once_and_fills_each_day(db, monkeypatch):
    from zoneinfo import ZoneInfo
    rows = [{"id": 1, "day": "2026-10-12", "list_title": "Daily", "title": "Run", "is_completed": True}]
    fake = db([_task(1, "Run", sort_order=1)], rows)
    monkeypatch.setattr(server, "_app_tz", lambda: ZoneInfo("America/Los_Angeles"))
    monkeypatch.setattr(server, "_shape", lambda r: dict(r))
    out = server.api_range("2026-10-12", "2026-10-14", Response())
    assert list(out["days"]) == ["2026-10-12", "2026-10-13", "2026-10-14"]
    assert out["days"]["2026-10-12"][0]["is_completed"] is True
    assert out["days"]["2026-10-14"][0]["virtual"] is True
    with pytest.raises(HTTPException):
        server.api_range("2026-01-01", "2026-12-31", Response())


def test_ticking_a_scheduled_task_makes_its_row(db, monkeypatch):
    fake = db([_task(1, "Run", sort_order=1), _task(2, "Floss", sort_order=2)])
    monkeypatch.setattr(server, "_day_rows", lambda client, day: [
        r for r in fake.tables["task_history"] if r["day"] == day])
    server.api_check("2026-10-13", "Run", True)
    assert [(r["title"], r["is_completed"]) for r in fake.tables["task_history"]] == [("Run", True)]
    with pytest.raises(HTTPException):
        server.api_check("2026-10-14", "Run", True)      # hasn't happened yet
    with pytest.raises(HTTPException):
        server.api_check("2026-10-13", "Not a task", True)


def test_consistency_counts_unopened_days_as_owed(db, monkeypatch):
    db([_task(1, "Run", sort_order=1, starts_on="2026-10-11")])
    monkeypatch.setattr(server, "_task_projection", lambda cols, title=None: [
        {"day": "2026-10-11", "list_title": "Daily", "title": "Run", "is_completed": True}])
    out = sorted((r["day"], r["is_completed"]) for r in server.api_consistency())
    assert out == [("2026-10-11", True), ("2026-10-12", False), ("2026-10-13", False)]


def test_today_page_keeps_today_current_after_midnight():
    """An open tab must not keep the day it loaded on as 'today'.

    It used to: overnight, the new day counted as one that hadn't happened, so
    its tasks were locked until a reload.
    """
    html = server.page_today("2026-10-16")
    assert '"tz":"' in html
    assert "function todayNow()" in html
    assert "function rollover()" in html
    assert "> BOOT.today" not in html          # no comparisons against the load-time date
    assert "BOOT.writable" not in html         # nor the load-time day's writability


def test_every_day_read_is_live_and_day_scoped(monkeypatch):
    """A prefetch used to answer from the per-instance history cache.

    On Vercel each instance holds its own copy, up to fifteen minutes old, so a
    prefetched day painted stale and flipped when the live read landed; on a
    cold instance it also downloaded every row to return one day.
    """
    monkeypatch.setattr(server, "_definitions", lambda fresh=False: [])
    monkeypatch.setattr(server, "_sb", lambda: object())
    monkeypatch.setattr(server, "_day_rows", lambda client, day: [])
    monkeypatch.setattr(
        server, "_task_history",
        lambda: (_ for _ in ()).throw(AssertionError("a day read used the history cache")))
    for fresh in (True, False):
        server.api_day("2026-10-19", Response(), fresh=fresh)


def test_day_switch_is_one_paint():
    html = server.page_today("2026-10-19")
    # Neighbouring days come live, three each side, in one request.
    assert "var PREFETCH = 3" in html and "'/api/range?start=' + start" in html
    assert "fetchDay(d, false)" not in html
    # No fade on a switch: only a tap animates.
    assert "body.painting .task, body.painting .field" in html
