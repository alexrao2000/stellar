"""A minimal PostgREST client, standing in for the `supabase` SDK on the hot path.

Importing `supabase` costs ~680ms. It is a lazy import, so it is not paid at
module load -- it is paid on the first request that touches the database, which
is every request that matters. On a Vercel cold start that landed on top of the
~680ms already spent importing FastAPI, and showed up as the second or so of
dead time before a day's tasks appeared.

The app uses a small corner of that SDK: select/insert/update/delete against
four tables, with eq/is_/order/range and an exact count. All of it is plain
PostgREST over HTTP, and httpx is already imported for auth. So this trades a
680ms import for a few hundred lines that speak the same protocol.

The connection pool is a second, quieter win: a module-level client keeps TLS
sessions alive between requests on a warm instance, where the SDK built a fresh
client -- and a fresh handshake -- per call.

Deliberately NOT a general-purpose client. It covers what the call sites use and
raises on anything else, so a new query shape fails loudly here rather than
silently returning the wrong rows. The offline sync, the keepalive cron and the
scripts still use the real SDK; they are not latency-sensitive and their breadth
is not worth re-implementing.
"""
from __future__ import annotations

import httpx

# Kept for the lifetime of the process. On Vercel that means a warm instance
# reuses the TLS session across requests instead of re-handshaking per query.
_client: httpx.Client | None = None


def _http() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=30.0)
    return _client


class PostgrestError(RuntimeError):
    """A non-2xx from PostgREST, carrying the body so the cause is visible."""


class Result:
    """The shape the call sites read off `.execute()`: `.data` and `.count`."""

    __slots__ = ("data", "count")

    def __init__(self, data: list[dict], count: int | None = None) -> None:
        self.data = data
        self.count = count


class _Query:
    def __init__(self, base: str, key: str, table: str) -> None:
        self._base = base.rstrip("/")
        self._key = key
        self._table = table
        self._method = "GET"
        self._select = "*"
        self._body: object = None
        # A list, not a dict: two filters on one column would collide in a dict,
        # and PostgREST treats repeated params as an AND.
        self._filters: list[tuple[str, str]] = []
        self._order: str | None = None
        self._limit: int | None = None
        self._range: tuple[int, int] | None = None
        self._count: bool = False

    # -- builders ---------------------------------------------------------- #
    def select(self, columns: str = "*", count: str | None = None) -> "_Query":
        self._method = "GET"
        self._select = columns
        if count is not None:
            if count != "exact":
                raise ValueError(f"unsupported count mode: {count!r}")
            self._count = True
        return self

    def insert(self, values) -> "_Query":
        self._method = "POST"
        self._body = values
        return self

    def update(self, values: dict) -> "_Query":
        self._method = "PATCH"
        self._body = values
        return self

    def delete(self) -> "_Query":
        self._method = "DELETE"
        return self

    def eq(self, column: str, value) -> "_Query":
        # PostgREST spells booleans lowercase; Python's str() would send "True".
        if isinstance(value, bool):
            value = "true" if value else "false"
        self._filters.append((column, f"eq.{value}"))
        return self

    def lt(self, column: str, value) -> "_Query":
        self._filters.append((column, f"lt.{value}"))
        return self

    def filter(self, column: str, operator: str, value) -> "_Query":
        """Add a raw PostgREST operator, matching supabase-py's API."""
        if not operator.strip():
            raise ValueError(f"invalid PostgREST operator: {operator!r}")
        self._filters.append((column, f"{operator}.{value}"))
        return self

    def is_(self, column: str, value: str) -> "_Query":
        self._filters.append((column, f"is.{value}"))
        return self

    def order(self, column: str, desc: bool = False) -> "_Query":
        self._order = f"{column}.desc" if desc else column
        return self

    def limit(self, count: int) -> "_Query":
        if count < 0:
            raise ValueError(f"limit must be non-negative: {count}")
        self._limit = count
        return self

    def range(self, start: int, end: int) -> "_Query":
        self._range = (start, end)
        return self

    # -- execution --------------------------------------------------------- #
    def execute(self) -> Result:
        params: list[tuple[str, str]] = []
        if self._method == "GET":
            params.append(("select", self._select))
            if self._order:
                params.append(("order", self._order))
            if self._limit is not None:
                params.append(("limit", str(self._limit)))
        params.extend(self._filters)

        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
        }
        prefer: list[str] = []
        if self._method in ("POST", "PATCH", "DELETE"):
            # The call sites read `.data` off writes, so ask for the rows back.
            prefer.append("return=representation")
        if self._count:
            prefer.append("count=exact")
        if prefer:
            headers["Prefer"] = ",".join(prefer)
        if self._range is not None:
            headers["Range"] = f"{self._range[0]}-{self._range[1]}"
        if self._body is not None:
            headers["Content-Type"] = "application/json"

        url = f"{self._base}/rest/v1/{self._table}"
        try:
            response = _http().request(
                self._method, url, params=params, headers=headers,
                json=self._body if self._body is not None else None,
            )
        except httpx.HTTPError as exc:
            raise PostgrestError(
                f"{self._method} {self._table} failed: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise PostgrestError(
                f"{self._method} {self._table} -> {response.status_code}: {response.text}"
            )

        data = response.json() if response.content else []
        if isinstance(data, dict):
            data = [data]

        # "0-999/6221" -- the total lives after the slash. "*" means unknown,
        # which read_rows() handles by falling back to sequential paging.
        count = None
        if self._count:
            total = response.headers.get("content-range", "").partition("/")[2]
            if total.isdigit():
                count = int(total)
        return Result(data, count)


class Client:
    """Drop-in for the slice of `supabase.Client` the request path uses."""

    def __init__(self, url: str, key: str) -> None:
        self._url = url
        self._key = key

    def table(self, name: str) -> _Query:
        return _Query(self._url, self._key, name)


def create_client(url: str, key: str) -> Client:
    return Client(url, key)
