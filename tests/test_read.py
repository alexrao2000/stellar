from types import SimpleNamespace

from src.config import Config
from src.read import read_rows


def test_read_rows_fetches_large_tables_in_order(monkeypatch):
    total = 2500
    starts = []

    class Query:
        def __init__(self):
            self.start = 0
            self.end = 0
            self.with_count = False

        def select(self, columns, count=None):
            assert columns == "id,day"
            self.with_count = count == "exact"
            return self

        def eq(self, column, value):
            assert (column, value) == ("title", "Wake Up")
            return self

        def range(self, start, end):
            self.start, self.end = start, end
            return self

        def execute(self):
            starts.append(self.start)
            stop = min(self.end + 1, total)
            data = [{"id": i, "day": "2026-08-19"} for i in range(self.start, stop)]
            return SimpleNamespace(data=data, count=total if self.with_count else None)

    class Client:
        def table(self, name):
            assert name == "task_history"
            return Query()

    # read_rows imports create_client from src.postgrest at call time, so
    # patching the attribute is enough -- no sys.modules surgery needed.
    import src.postgrest

    monkeypatch.setattr(src.postgrest, "create_client", lambda url, key: Client())
    config = Config(supabase_url="https://project.supabase.co", supabase_key="secret")

    rows = read_rows(
        config,
        "task_history",
        columns="id,day",
        equals={"title": "Wake Up"},
    )

    assert [row["id"] for row in rows] == list(range(total))
    assert sorted(starts) == [0, 1000, 2000]
