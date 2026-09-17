from src import postgrest
import httpx


def test_query_lt_adds_less_than_filter(monkeypatch):
    request = {}

    class Response:
        status_code = 200
        content = b"[]"
        headers = {}

        def json(self):
            return []

    class Http:
        def request(self, method, url, **kwargs):
            request.update(method=method, url=url, **kwargs)
            return Response()

    monkeypatch.setattr(postgrest, "_http", lambda: Http())

    postgrest._Query("https://example.supabase.co", "secret", "task_history") \
        .select("day,weight_lbs") \
        .lt("day", "2026-09-08") \
        .execute()

    assert ("day", "lt.2026-09-08") in request["params"]


def test_query_filter_adds_postgrest_operator_filter(monkeypatch):
    request = {}

    class Response:
        status_code = 200
        content = b"[]"
        headers = {}

        def json(self):
            return []

    class Http:
        def request(self, _method, _url, **kwargs):
            request.update(kwargs)
            return Response()

    monkeypatch.setattr(postgrest, "_http", lambda: Http())

    postgrest._Query("https://example.supabase.co", "secret", "task_history") \
        .select("day,weight_lbs") \
        .filter("weight_lbs", "not.is", "null") \
        .execute()

    assert ("weight_lbs", "not.is.null") in request["params"]


def test_query_limit_adds_limit_parameter(monkeypatch):
    request = {}

    class Response:
        status_code = 200
        content = b"[]"
        headers = {}

        def json(self):
            return []

    class Http:
        def request(self, _method, _url, **kwargs):
            request.update(kwargs)
            return Response()

    monkeypatch.setattr(postgrest, "_http", lambda: Http())

    postgrest._Query("https://example.supabase.co", "secret", "task_history") \
        .select("day,weight_lbs") \
        .limit(1) \
        .execute()

    assert ("limit", "1") in request["params"]


def test_query_wraps_transport_failures_with_context(monkeypatch):
    class Http:
        def request(self, *_args, **_kwargs):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(postgrest, "_http", lambda: Http())

    try:
        postgrest._Query("https://example.supabase.co", "secret", "task_history").select().execute()
    except postgrest.PostgrestError as exc:
        assert "GET task_history failed" in str(exc)
        assert isinstance(exc.__cause__, httpx.ConnectError)
    else:
        raise AssertionError("transport failure escaped without context")
