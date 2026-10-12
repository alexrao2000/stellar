"""Local app server. Serves the interfaces, reads Supabase, and records check-ins.

The pages fetch same-origin from this server rather than talking to Supabase
directly, which means the database key never reaches the browser. That also
removes the need for an anon key and for a public read policy on task_history:
the credential stays in this process.

Reads are cached; writes are bounded to task_history and to the fields a daily
check-in touches. Nothing here writes to Google.

Run:  make dev        (or: uvicorn src.server:app --reload --port 8000)
"""
from __future__ import annotations

import logging
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from .config import load_config
from .parsers import parse_wake_up
from .postgrest import PostgrestError
from .read import read_rows
from .web_auth import AuthError, AuthSession, web_auth

log = logging.getLogger("cal-task-sync")

TEMPLATES = Path(__file__).resolve().parent.parent / "scripts" / "templates"

# Same clause the reconciler rewrites, so the note format stays consistent
# between historical repair and new entries.
CURRENT_WEIGHT_CLAUSE = re.compile(
    r"current\s*weight\s*:\s*[\d.]*\s*(?:lbs?)?", re.IGNORECASE
)

# A full read is ~1.8 MB over seven sequential round trips, about two seconds.
# Writes are folded into the cache rather than dropping it, so it stays correct
# between refreshes and can be held far longer than the original two minutes.
# POST /api/refresh forces a re-read.
CACHE_TTL = 900.0
# Pool state is also changed by other machines. Its page polls while visible,
# so keep this cache short enough for those polls to observe remote writes while
# still collapsing simultaneous page loads into one pair of Supabase reads.
POOL_CACHE_TTL = 5.0
# After a refresh fails, keep serving what we have for this long before trying
# again. Without it a dead network is re-dialled on every single request, and
# /today now asks for fifteen days at once.
STALE_RETRY = 30.0
_cache: dict[str, tuple[float, object]] = {}
_refresh_failed: dict[str, float] = {}

# docs_url=None: /docs is served below instead, so that the API reference gets
# the same sidebar as every other view. FastAPI's own page has no way back to
# the app, which made the nav's last link a one-way door.
app = FastAPI(title="Task Tracker", docs_url=None)


@app.exception_handler(PostgrestError)
async def handle_postgrest_error(_request: Request, exc: PostgrestError):
    """Turn database/transport failures into a safe, retryable API response."""
    log.error("database request failed: %s", exc)
    return JSONResponse(
        {"detail": "Database temporarily unavailable; please retry."},
        status_code=503,
    )

# The history payload is ~1.8 MB of highly repetitive JSON. Compressed it is a
# fraction of that, which is most of the cold-load cost on any connection
# slower than localhost.
app.add_middleware(GZipMiddleware, minimum_size=1000)

ACCESS_COOKIE = "task_access"
REFRESH_COOKIE = "task_refresh"
PUBLIC_PATHS = {"/favicon.svg", "/login", "/auth/login", "/auth/logout"}


def _auth_configured(config) -> bool:
    return bool(
        config.supabase_url
        and config.supabase_anon_key
        and config.auth_allowed_email
    )


def _allowed(config, user) -> bool:
    return user.email.casefold() == config.auth_allowed_email


def _set_session_cookies(response: Response, session: AuthSession, config) -> None:
    common = {
        "httponly": True,
        "secure": config.auth_cookie_secure,
        "samesite": "lax",
        "path": "/",
    }
    response.set_cookie(
        ACCESS_COOKIE, session.access_token, max_age=session.expires_in, **common
    )
    # Supabase owns session expiry and rotates this value on every refresh. A
    # durable browser cookie prevents an arbitrary client-side timeout from
    # ending an otherwise valid session.
    response.set_cookie(
        REFRESH_COOKIE, session.refresh_token, max_age=365 * 24 * 60 * 60, **common
    )


def _clear_session_cookies(response: Response, config) -> None:
    for name in (ACCESS_COOKIE, REFRESH_COOKIE):
        response.delete_cookie(
            name,
            path="/",
            secure=config.auth_cookie_secure,
            httponly=True,
            samesite="lax",
        )


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return False
    forwarded_proto = request.headers.get("x-forwarded-proto")
    scheme = (forwarded_proto or request.url.scheme).split(",", 1)[0].strip()
    return origin.rstrip("/") == f"{scheme}://{request.headers.get('host', '')}"


def _auth_failure(request: Request, detail: str = "Sign in required", status: int = 401):
    if request.method in {"GET", "HEAD"} and not request.url.path.startswith("/api/"):
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
    return JSONResponse({"detail": detail}, status_code=status)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        config = load_config()
        if not config.auth_enabled or request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        if not _auth_configured(config):
            return JSONResponse(
                {"detail": "Authentication is enabled but not configured"}, status_code=503
            )
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not _same_origin(request):
            return JSONResponse({"detail": "Cross-origin write refused"}, status_code=403)

        access = request.cookies.get(ACCESS_COOKIE, "")
        refresh = request.cookies.get(REFRESH_COOKIE, "")
        renewed = None
        user = None
        if access:
            try:
                user = await web_auth.verify(config.supabase_url, config.supabase_anon_key, access)
            except AuthError as exc:
                if exc.status == 503:
                    return JSONResponse({"detail": str(exc)}, status_code=503)
        if user is None and refresh:
            try:
                renewed = await web_auth.refresh(
                    config.supabase_url, config.supabase_anon_key, refresh
                )
                user = renewed.user
            except AuthError as exc:
                if exc.status == 503:
                    return JSONResponse({"detail": str(exc)}, status_code=503)
        if user is None:
            response = _auth_failure(request)
            _clear_session_cookies(response, config)
            return response
        if not _allowed(config, user):
            response = _auth_failure(request, "This account is not allowed", 403)
            _clear_session_cookies(response, config)
            return response

        request.state.auth_user = user
        response = await call_next(request)
        if renewed:
            _set_session_cookies(response, renewed, config)
        return response


app.add_middleware(AuthMiddleware)


class LoginBody(BaseModel):
    email: str
    password: str


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def page_login(request: Request) -> Response:
    config = load_config()
    if not config.auth_enabled:
        return RedirectResponse("/", status_code=303)
    target = request.query_params.get("next", "/")
    if not target.startswith("/") or target.startswith("//"):
        target = "/"
    html = (TEMPLATES / "login.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__NEXT__", json.dumps(target)))


@app.post("/auth/login", include_in_schema=False)
async def auth_login(body: LoginBody, request: Request) -> Response:
    config = load_config()
    if not config.auth_enabled or not _auth_configured(config):
        raise HTTPException(503, "Authentication is not configured")
    if not _same_origin(request):
        raise HTTPException(403, "Cross-origin sign-in refused")
    try:
        session = await web_auth.sign_in(
            config.supabase_url, config.supabase_anon_key, body.email, body.password
        )
    except AuthError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    if not _allowed(config, session.user):
        raise HTTPException(403, "This account is not allowed")
    response = JSONResponse({"ok": True, "email": session.user.email})
    _set_session_cookies(response, session, config)
    return response


@app.post("/auth/logout", include_in_schema=False)
def auth_logout(request: Request) -> Response:
    config = load_config()
    if config.auth_enabled and not _same_origin(request):
        raise HTTPException(403, "Cross-origin sign-out refused")
    response = RedirectResponse("/login", status_code=303)
    _clear_session_cookies(response, config)
    return response


@app.on_event("startup")
def _warm_cache() -> None:
    """Pull the history in the background as soon as the server comes up.

    Otherwise the first page load after `make dev` pays the full read. Done on a
    thread so startup is not blocked, and failures are logged rather than raised
    -- an unreachable database should not stop the server booting, since the
    error pages explain the problem better than a crash does.
    """
    # A Vercel instance should do only the work needed by its request. Warming
    # the 6,000-row history on every serverless cold start multiplied database
    # traffic and made even /login and /today compete with a multi-page read.
    # Long-lived local development still benefits from warming in the background.
    if os.getenv("VERCEL"):
        return

    import threading

    def go():
        try:
            _task_history()
        except Exception as exc:  # noqa: BLE001
            log.warning("cache warm failed (%s); first request will be slower", exc)
        try:
            _pool_all()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=go, daemon=True).start()


def _cached(key: str, build, ttl: float = CACHE_TTL):
    """Read through the cache, and prefer a stale answer to no answer.

    These are reads of data that changes rarely -- the history, the recurrence
    definitions, their sort order. A momentary DNS failure used to turn the
    first request after the TTL lapsed into a 500, on the hottest path there
    is: /api/day goes through _definitions(), so every fifteen minutes one day
    view was one blip away from an error page. Writes are deliberately not
    covered by this; a save that cannot reach the database must still fail.
    """
    now = time.monotonic()
    hit = _cache.get(key)
    if hit:
        if now - hit[0] < ttl:
            return hit[1]
        if now - _refresh_failed.get(key, float("-inf")) < STALE_RETRY:
            return hit[1]                     # backing off a failing refresh
    try:
        value = build()
    except Exception as exc:  # noqa: BLE001
        if hit is None:
            raise                             # nothing to fall back on
        _refresh_failed[key] = now
        log.warning("refresh of %s failed (%s); serving the cached copy", key, exc)
        return hit[1]
    _refresh_failed.pop(key, None)
    _cache[key] = (now, value)
    return value


# Exactly what the pages consume. Whitelisted rather than passed through, so a
# new database column cannot silently start shipping to the browser.
HISTORY_COLS = (
    "id", "day", "list_title", "title", "is_completed", "completed_at",
    "notes", "weight_lbs", "weight_source", "takeout_weight",
)


def _shape(r: dict) -> dict:
    """One database row in the shape the pages consume."""
    rec = {c: r.get(c) for c in HISTORY_COLS}
    rec["day"] = str(rec["day"] or "")[:10]
    rec["is_completed"] = bool(r.get("is_completed"))
    # numeric comes back as a string from PostgREST
    for numeric in ("weight_lbs", "takeout_weight"):
        v = rec[numeric]
        rec[numeric] = None if v is None or v == "" else float(v)
    return rec


def _task_history() -> list[dict]:
    def build():
        rows = [_shape(r) for r in read_rows(load_config(), "task_history")]
        rows.sort(key=lambda r: (r["day"], r["title"]))
        return rows

    return _cached("task_history", build)


def _cache_apply(changed: list[dict]) -> None:
    """Fold a write into the cached history rather than dropping the cache.

    Clearing meant a single checkbox tap cost a full 6,221-row refetch on the
    next page load -- about two seconds, on every interaction. The rows a write
    touches are known, so patch those and leave the rest alone. The cache
    timestamp is preserved so this does not extend the TTL indefinitely.
    """
    hit = _cache.get("task_history")
    if not hit:
        return
    ts, rows = hit
    index = {r["id"]: i for i, r in enumerate(rows) if r.get("id") is not None}
    dirty = False
    for raw in changed:
        rec = _shape(raw)
        pos = index.get(rec["id"])
        if pos is None:
            rows.append(rec)
        else:
            rows[pos] = rec
        dirty = True
    if dirty:
        rows.sort(key=lambda r: (r["day"], r["title"]))
        _cache["task_history"] = (ts, rows)


# A bare checkmark in the app's accent purple -- no plate behind it, so the tab
# reads the same on a light or a dark browser chrome. Split between the two
# theme purples would be worse than either: the tab bar does not necessarily
# match the page.
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<path d="M5 17l8 8L27 8" fill="none" stroke="#7A4FD8" stroke-width="5.5"'
    ' stroke-linecap="round" stroke-linejoin="round"/></svg>'
)

_FAVICON_LINK = '<link rel="icon" href="/favicon.svg" type="image/svg+xml">'


@app.get("/favicon.svg")
def favicon() -> Response:
    return Response(
        FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# The one definition of the nav. It used to be copied into all five templates,
# which is how /docs ended up linked from the check-in page and nowhere else.
NAV_ITEMS = (
    ("/", "Today"),
    ("/calendar", "Calendar"),
    ("/weight", "Weight"),
    ("/wake", "Wake"),
    ("/consistency", "Consistency"),
    ("/data", "Data"),
    ("/pool", "Pool"),
    ("/tasks", "Settings"),
    ("/docs", "API"),
)

# Only variables every template already defines are used here, so the sidebar
# picks up each page's light/dark palette without carrying its own.
_NAV_CSS = """<style>
:root { --nav-w: 172px; --nav-accent: #7A4FD8; }
/* Fallback palette for a page that brings none of its own -- /docs is Swagger
   UI, not one of our templates. Declared first, so a template's own :root,
   which comes later in the document, overrides every one of these. */
:root {
  --surface: #FCFBFE; --surface-2: #F4F2F9; --rule: #DCD8E6;
  --ink: #191627; --ink-muted: #6A6480;
}
@media (prefers-color-scheme: dark) {
  :root {
    --surface: #1B1826; --surface-2: #232032; --rule: #2E2A3D;
    --ink: #EBE8F5; --ink-muted: #948DAD;
  }
}
/* `html body` beats the templates' own `html, body { padding: 0 }`, so this
   holds no matter which order the two stylesheets land in. */
html body { padding-left: var(--nav-w); }
/* Stated, not assumed. The templates all set `* { box-sizing: border-box }`,
   but /docs is Swagger UI and does not -- there the 172px width gained its
   padding and border on top, making the column 193px while the body was only
   offset 172, and tucking 21px of the page under it. */
.sidenav, .sidenav * { box-sizing: border-box; }
.sidenav {
  position: fixed; top: 0; bottom: 0; left: 0; width: var(--nav-w); z-index: 20;
  background: var(--surface); border-right: 1px solid var(--rule);
  display: flex; flex-direction: column; gap: 1px;
  padding: 18px 10px; overflow-y: auto;
  /* Stated rather than inherited: on /docs the surrounding page is Swagger UI,
     which sets its own family and root size, and the nav picked both up. */
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 14px; line-height: 1.2;
}
.sidenav .brand {
  font-family: ui-monospace, Menlo, monospace; font-size: .74em;
  letter-spacing: .16em; text-transform: uppercase;
  color: var(--ink-muted); padding: 0 10px 14px;
}
.sidenav a {
  display: block; padding: 9px 10px; border-radius: 7px; font-size: 1em;
  color: var(--ink-muted); text-decoration: none;
  border-left: 2px solid transparent;
}
.sidenav a:hover { background: var(--surface-2); color: var(--ink); }
.sidenav a.here {
  color: var(--ink); font-weight: 600;
  background: var(--surface-2); border-left-color: var(--nav-accent);
}
.sidenav a:focus-visible { outline: 2px solid var(--nav-accent); outline-offset: -2px; }
.sidenav .signout { margin-top: auto; padding-top: 12px; }
.sidenav .signout button {
  width: 100%; padding: 9px 10px; border: 0; border-radius: 7px;
  background: transparent; color: var(--ink-muted); cursor: pointer;
  font: inherit; text-align: left;
}
.sidenav .signout button:hover { background: var(--surface-2); color: var(--ink); }
.sidenav .signout button:focus-visible {
  outline: 2px solid var(--nav-accent); outline-offset: -2px;
}
/* The check-in page is phone-first -- it gets used standing in a bathroom. A
   fixed column would eat a third of that screen, so below this width the nav
   goes back to a scrollable strip across the top. */
@media (max-width: 760px) {
  html body { padding-left: 0; }
  .sidenav {
    position: sticky; top: 0; bottom: auto; width: auto;
    flex-direction: row; gap: 2px; padding: 7px 10px;
    border-right: none; border-bottom: 1px solid var(--rule);
    overflow-x: auto; overflow-y: hidden;
  }
  .sidenav .brand { display: none; }
  .sidenav a {
    white-space: nowrap; padding: 7px 10px;
    border-left: none; border-bottom: 2px solid transparent;
  }
  .sidenav a.here { border-left-color: transparent; border-bottom-color: var(--nav-accent); }
  .sidenav .signout { margin-top: 0; padding-top: 0; }
  .sidenav .signout button { white-space: nowrap; padding: 7px 10px; }
}
@media print { .sidenav { display: none; } html body { padding-left: 0; } }

/* Hold the real content back until its data is in and stand a skeleton in its
   place, so nothing is ever on screen showing an empty count or a blank stat.
   The sidebar is exempt -- it needs no data, so the frame is up immediately.

   `visibility`, not `display`. The weight chart measures its container and
   draws nothing at zero size, which is exactly what `display: none` would
   hand it -- hidden layout is still laid out. */
html.app-booting body > *:not(.sidenav):not(.app-skeleton) { visibility: hidden; }
html:not(.app-booting) .app-skeleton { display: none; }

/* Pinned to the content area rather than placed in the flow. Hidden content is
   still laid out, so a skeleton appended after it starts one full page-height
   down -- off screen on any page with a long table. Its inner box copies the
   page's own .wrap at runtime, so it lands where the real content will rather
   than approximating it. */
.app-skeleton {
  position: fixed; top: 0; left: var(--nav-w); right: 0; bottom: 0;
  overflow: hidden; box-sizing: border-box; z-index: 10;
}
.app-skeleton .sk-inner { margin: 0 auto; box-sizing: border-box; }
@media (max-width: 760px) { .app-skeleton { left: 0; } }
/* Mixed from --ink rather than set to --surface-2: that variable is only a
   shade off --bg in the light theme, and the bars all but vanished. */
.app-skeleton .sk {
  background: color-mix(in srgb, var(--ink) 9%, transparent);
  border-radius: 8px; position: relative; overflow: hidden;
}
/* --surface sits above --bg in both themes, so the sweep lightens either way. */
.app-skeleton .sk::after {
  content: ""; position: absolute; inset: 0; transform: translateX(-100%);
  background: linear-gradient(90deg, transparent,
    color-mix(in srgb, var(--surface) 70%, transparent), transparent);
  animation: sk-shimmer 1.25s ease-in-out infinite;
}
@keyframes sk-shimmer { to { transform: translateX(100%); } }
.app-skeleton .sk-eyebrow { height: 9px;  width: 92px;  margin-bottom: 12px; }
.app-skeleton .sk-title   { height: 27px; width: 210px; margin-bottom: 11px; }
.app-skeleton .sk-sub     { height: 12px; width: 320px; max-width: 80%; margin-bottom: 24px; }
.app-skeleton .sk-card    { height: 116px; margin-bottom: 14px; }
.app-skeleton .sk-card-tall { height: 300px; }
/* A shimmer that cannot be turned off is worse than none. */
@media (prefers-reduced-motion: reduce) { .app-skeleton .sk::after { animation: none; } }
</style>"""


# Wrapping fetch beats wiring five templates by hand -- the same reason the nav
# moved here, and it covers a page added later without anyone remembering to.
#
# The class is set from script rather than shipped in the markup on purpose: if
# scripting is off or this throws, nothing is ever hidden. Failing to a visible
# page beats failing to a blank one.
_BOOT_GATE_JS = """<script>
(function () {
  var root = document.documentElement;
  if (!window.fetch) return;                 // no gate we could reliably lift
  root.classList.add('app-booting');

  var inflight = 0, started = false, done = false, quiet, giveup;
  var real = window.fetch;

  function reveal() {
    if (done) return;
    done = true;
    clearTimeout(quiet); clearTimeout(giveup);
    window.fetch = real;
    root.classList.remove('app-booting');
    var sk = document.querySelector('.app-skeleton');
    if (sk) sk.parentNode.removeChild(sk);
  }

  /* Built here rather than shipped in the markup because it copies its box
     from the page's own .wrap -- same max-width, same padding, same centring
     -- so it occupies exactly the space the real content is about to. */
  function skeleton() {
    if (done || document.querySelector('.app-skeleton')) return;
    var host = document.querySelector('.wrap');
    var cs = host ? getComputedStyle(host) : null;
    var inner = document.createElement('div');
    inner.className = 'sk-inner';
    inner.style.maxWidth = cs && cs.maxWidth !== 'none' ? cs.maxWidth : '1100px';
    inner.style.paddingTop = cs ? cs.paddingTop : '28px';
    inner.style.paddingLeft = cs ? cs.paddingLeft : '18px';
    inner.style.paddingRight = cs ? cs.paddingRight : '18px';
    inner.innerHTML =
      '<div class="sk sk-eyebrow"></div>' +
      '<div class="sk sk-title"></div>' +
      '<div class="sk sk-sub"></div>' +
      '<div class="sk sk-card"></div>' +
      '<div class="sk sk-card sk-card-tall"></div>';

    var el = document.createElement('div');
    el.className = 'app-skeleton';
    el.setAttribute('aria-hidden', 'true');
    /* Below 760px the nav is a strip across the top rather than a column, so
       start beneath it instead of underneath it. Measured, not guessed. */
    var nav = document.querySelector('.sidenav');
    if (nav && getComputedStyle(nav).position !== 'fixed') {
      el.style.top = Math.round(nav.getBoundingClientRect().height) + 'px';
    }
    el.appendChild(inner);
    document.body.appendChild(el);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', skeleton);
  } else {
    skeleton();
  }

  function settle() {
    if (--inflight > 0) return;
    /* /today posts the day into existence before reading it back, and must not
       be revealed in between. The measured gap between those two is 1ms; 40
       leaves two orders of magnitude of headroom and still costs a hundred
       less than the 140 this started at, which had become the largest single
       component of a fast load. */
    quiet = setTimeout(reveal, 40);
  }

  window.fetch = function () {
    started = true;
    inflight++;
    clearTimeout(quiet);
    return real.apply(this, arguments).then(
      function (r) { settle(); return r; },
      function (e) { settle(); throw e; }
    );
  };

  /* Only the opening burst gates the page. Once revealed the wrapper takes
     itself off, so the write behind a checkbox tap can never blank the view --
     that interaction was made instant deliberately.

     Both of these are the reason a page cannot get stuck invisible: one for a
     page that never fetches, one for a request that hangs. */
  giveup = setTimeout(reveal, 10000);
  window.addEventListener('load', function () {
    setTimeout(function () { if (!started) reveal(); }, 300);
  });
})();
</script>"""


def _nav(active: str) -> str:
    links = "".join(
        '<a href="{}"{}>{}</a>'.format(
            href, ' class="here"' if href == active else "", label
        )
        for href, label in NAV_ITEMS
    )
    signout = ""
    if load_config().auth_enabled:
        signout = (
            '<form class="signout" action="/auth/logout" method="post">'
            '<button type="submit">Sign out</button></form>'
        )
    return (
        '<nav class="sidenav" aria-label="Views">'
        '<div class="brand">Task Tracker</div>' + links + signout + "</nav>"
    )


@app.get("/docs", include_in_schema=False)
def swagger_docs() -> HTMLResponse:
    """FastAPI's API reference, with the app's sidebar on it.

    The stock page is a dead end: nothing on it links back, so the nav's last
    link dropped you out of the app. Rather than open it in a new tab -- which
    only hides the problem -- the generated page is served here with the nav
    injected, and it behaves like every other view.
    """
    from fastapi.openapi.docs import get_swagger_ui_html

    page = get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title="Task Tracker API",
        swagger_favicon_url="/favicon.svg",
    ).body.decode()
    # Swagger UI is a complete document, DOCTYPE included. Prepending to it the
    # way _render does would put the page in quirks mode, so this goes just
    # inside <body> instead.
    if "<body>" not in page:  # pragma: no cover - guards a FastAPI upgrade
        log.warning("swagger html shape changed; serving /docs without the nav")
        return HTMLResponse(page)
    shell = _NAV_CSS + _nav("/docs") + _BOOT_GATE_JS
    return HTMLResponse(page.replace("<body>", "<body>" + shell, 1))


def _render(template: str, boot: dict, active: str = "") -> str:
    import json

    path = TEMPLATES / template
    if not path.exists():
        raise HTTPException(500, f"template missing: {template}")
    html = path.read_text(encoding="utf-8").replace(
        "__BOOTSTRAP__", json.dumps(boot, separators=(",", ":"))
    )
    # A page whose data is already in the markup has nothing to wait for, and
    # gating it would be strictly worse than not: the gate lifts on the first
    # quiet network, and with no request to wait on that is a timer rather than
    # an event. Such a page paints complete on the first frame.
    gate = "" if boot.get("mode") == "inline" else _BOOT_GATE_JS
    # Injected here rather than in each template: every page goes through this
    # one function, so there is no way to add a view and forget the icon or the
    # nav, or to leave one page's copy of the link list behind when it changes.
    return _FAVICON_LINK + _NAV_CSS + _nav(active) + gate + html


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/task_history")
def api_task_history() -> list[dict]:
    """The reconciled record. Returned whole -- the client does not page this."""
    return _task_history()


def _task_projection(columns: str, *, title: str | None = None) -> list[dict]:
    """Read a narrow task_history projection without loading the full cache."""
    config = load_config()
    equals = {"title": title} if title is not None else None
    return read_rows(config, "task_history", columns=columns, equals=equals)


@app.get("/api/weights")
def api_weights() -> list[dict]:
    """Per-day weights, taken from task_history rather than weight_log.

    task_history is the reconciled record, so it includes the days that were
    supplied after the overwrite and that weight_log never captured.
    """
    wake_or_weight = [
        r for r in _task_projection("day,weight_lbs", title="Weigh yourself")
        if r.get("weight_lbs") is not None
    ]
    return [{"d": r["day"], "w": round(float(r["weight_lbs"]), 1)} for r in wake_or_weight]


@app.get("/api/wake")
def api_wake() -> list[dict]:
    """Wake-up times parsed out of task_history notes.

    Reuses src.parsers.parse_wake_up rather than re-implementing the regex --
    a second copy here silently returned nothing, having lost the IGNORECASE
    flag that lets it match "Wake-up Time:" as actually written.
    """
    out = []
    for r in _task_projection("day,notes", title="Wake Up"):
        if not r.get("notes"):
            continue
        parsed = parse_wake_up(r["notes"])
        if parsed:
            # `w` so the shared chart can read both series without branching;
            # `time` is kept for anything reading this endpoint directly.
            out.append({"d": r["day"], "time": parsed[0], "w": parsed[1]})
    return out


@app.get("/api/consistency")
def api_consistency() -> list[dict]:
    """The fields used by consistency analysis, without notes or metrics.

    `optional` comes from the task's current definition: an unticked optional
    task is not a miss.
    """
    optional = {d["title"] for d in _definitions() if d.get("optional")}
    return [{**r, "optional": r["title"] in optional}
            for r in _task_projection("day,list_title,title,is_completed")]


@app.post("/api/refresh")
def api_refresh() -> dict:
    """Drop the cache so the next read hits Supabase.

    Writes patch the cache in place rather than clearing it; this is the escape
    hatch for when the database changed underneath us.
    """
    _cache.clear()
    _refresh_failed.clear()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Writes
#
# Everything below mutates task_history. Bounded on purpose: only that table,
# only rows for a given day, and only the fields a daily check-in touches.
# --------------------------------------------------------------------------- #
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# A weigh-in outside this range is a typo, not a reading. Refuse rather than
# write a number that would wreck the chart and need reconciling later.
WEIGHT_MIN, WEIGHT_MAX = 50.0, 500.0
WEIGHT_GAIN_WARNING = 20.0
WEIGHT_LOSS_RATIO_WARNING = 0.5


def _sb():
    # The local PostgREST shim, not the `supabase` SDK: importing that SDK costs
    # ~680ms, and being a lazy import only moved the cost onto the first request
    # that touches the database. See src/postgrest.py.
    from .postgrest import create_client

    config = load_config()
    if not config.supabase_url or not config.supabase_key:
        raise HTTPException(500, "SUPABASE_URL and a key are required in .env.")
    return create_client(config.supabase_url, config.supabase_key)


def _check_day(day: str) -> str:
    if not DAY_RE.match(day or ""):
        raise HTTPException(400, f"day must be YYYY-MM-DD, got {day!r}")
    return day


# Everything before this is the reconciled historical record. It took a long
# recovery to get right and nothing in a daily check-in has any business
# touching it, so writes to earlier days are refused outright.
#
# 2026-07-31 rather than 08-01: the app went live on the 31st Pacific, so that
# day is one the user records normally rather than part of the frozen history.
#
# Temporary by intent -- lift it deliberately when back-editing is wanted, not
# by accident. Reads are unaffected; the calendar still shows all of history.
WRITE_FLOOR = date(2026, 7, 31)


def _check_writable(day: str) -> str:
    _check_day(day)
    if date.fromisoformat(day) < WRITE_FLOOR:
        raise HTTPException(
            403,
            f"writes to {day} are disabled: everything before "
            f"{WRITE_FLOOR.isoformat()} is the reconciled history and is read-only.",
        )
    return day


def _day_rows(client, day: str) -> list[dict]:
    return (
        client.table("task_history").select("*").eq("day", day).execute().data or []
    )


def _weight_warning(lbs: float, previous: dict | None) -> str | None:
    """Describe an extreme change while allowing unusual real measurements."""
    if not previous or previous.get("weight_lbs") in (None, ""):
        return None
    prior = float(previous["weight_lbs"])
    prior_day = str(previous.get("day") or "the previous entry")[:10]
    change = lbs - prior
    if change > WEIGHT_GAIN_WARNING:
        return (
            f"Saved, but double-check: {lbs:.1f} lbs is {change:.1f} lbs above "
            f"your previous {prior:.1f} lbs on {prior_day}."
        )
    if lbs < prior * WEIGHT_LOSS_RATIO_WARNING:
        return (
            f"Saved, but double-check: {lbs:.1f} lbs is less than half your "
            f"previous {prior:.1f} lbs on {prior_day}."
        )
    return None


def _missing_recurrences(
    day: str, rows: list[dict], defs: list[dict] | None = None
) -> list[dict]:
    """Which recurrence definitions due on `day` have no row in `rows` yet.

    Shared deliberately. api_ensure_day fills the gap; api_day reports whether
    there is one so the page can skip the call. Two copies of this rule would
    eventually disagree, and the failure would be silent -- a day rendering one
    task short, with nothing raising. `defs` lets a caller that has just read
    the definitions pass them in rather than read them twice.
    """
    due = [
        d for d in (_recurring_defs() if defs is None else defs)
        if (not d.get("starts_on") or d["starts_on"] <= day)
        and (not d.get("ends_on") or d["ends_on"] >= day)
    ]
    present = {(r["list_title"], r["title"]) for r in rows}
    return [d for d in due if (d["list_title"], d["title"]) not in present]


@app.post("/api/day/{day}/ensure")
def api_ensure_day(day: str) -> dict:
    """Materialise a day's recurring occurrences if they don't exist yet.

    This is what replaces Google's recurrence. Insert-only: rows that already
    exist are left exactly as they are, so calling it repeatedly is harmless and
    can never clobber a completion or a weight already recorded.
    """
    _check_writable(day)

    if not _recurring_defs():
        raise HTTPException(500, "recurring_tasks is empty -- apply sql/schema.sql.")

    # Always inspect only this day. On a long-lived local process the full
    # history cache made this shortcut cheap, but on Vercel it forced each cold
    # instance to download thousands of rows just to answer whether today's
    # handful already exists.
    client = _sb()
    rows = _day_rows(client, day)
    existing = {(r["list_title"], r["title"]) for r in rows}
    wanted = _missing_recurrences(day, rows)
    if wanted:
        created = client.table("task_history").insert([
            {
                "day": day,
                "list_title": d["list_title"],
                "title": d["title"],
                "status": "needsAction",
                "is_completed": False,
            }
            for d in wanted
        ]).execute()
        _cache_apply(created.data or [])
    return {"day": day, "created": [d["title"] for d in wanted],
            "already_present": len(existing)}


@app.post("/api/ensure_range")
def api_ensure_range(start: str, end: str) -> dict:
    """Materialise every day in [start, end]. Insert-only, same as the single-day form.

    Deliberately not run automatically over the future: a pre-created row for a
    day that has not happened yet reads as an incomplete task, which is how the
    calendar ends up showing a wall of false misses. Today materialises itself on
    page load; use this to fill a gap.
    """
    _check_writable(start)
    _check_writable(end)
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    if d1 < d0:
        raise HTTPException(400, "end is before start")
    if (d1 - d0).days > 366:
        raise HTTPException(400, "range longer than 366 days; narrow it")

    created: dict[str, list[str]] = {}
    for i in range((d1 - d0).days + 1):
        day = (d0 + timedelta(days=i)).isoformat()
        result = api_ensure_day(day)
        if result["created"]:
            created[day] = result["created"]
    return {"start": start, "end": end, "days_touched": len(created), "created": created}


def _measurement_titles() -> set[str]:
    config = load_config()
    return {config.weight_task_title, config.wake_up_task_title}


def _definitions_order_key(d: dict, measurements: set[str]) -> tuple:
    """Where a definition sits on the Today page.

    An explicit sort_order -- set by dragging -- wins. Until one is set the
    definitions share sort_order 0 and fall back to the default: measurements,
    then required tasks, then optional ones, each group in the order the
    definitions were created.
    """
    return (
        d.get("sort_order") or 0,
        0 if d["title"] in measurements else (2 if d.get("optional") else 1),
        d.get("created_at") or "",
        d.get("id") or 0,
    )


def _definitions(fresh: bool = False) -> list[dict]:
    """Every recurrence definition, active or not, in the order the page lists them.

    One read serves both the order and materialising; there used to be two
    caches of this one 8-row table, and two queries on a cold instance.

    `fresh` reads it live. The visible day asks for that because the order is
    now editable: a cache per server instance would show a new order on one
    device and the old one on another for up to fifteen minutes. The read is a
    handful of rows from a database in the same region, and a failure falls
    back to whatever this instance last had.
    """
    def build():
        rows = _sb().table("recurring_tasks").select("*").execute().data or []
        measurements = _measurement_titles()
        return sorted(rows, key=lambda d: _definitions_order_key(d, measurements))

    if fresh:
        try:
            defs = build()
        except Exception as exc:  # noqa: BLE001
            if _cache.get("definitions") is None:
                raise
            log.warning("live read of definitions failed (%s); using the cached copy", exc)
            return _cache["definitions"][1]
        _cache["definitions"] = (time.monotonic(), defs)
        _refresh_failed.pop("definitions", None)
        return defs
    return _cached("definitions", build)


def _app_tz():
    """The timezone days are bucketed in -- the one /today uses for 'today'."""
    from zoneinfo import ZoneInfo

    from .timezones import resolve_timezone

    return ZoneInfo(resolve_timezone(load_config(), allow_google=False))


def _deadline_at(day: str, minutes: int | None, tz) -> str | None:
    """The instant a deadline falls, as UTC ISO; minutes past 1440 reach into the next day.

    Built from a local wall-clock time rather than by adding minutes to
    midnight, so a deadline on a daylight-saving changeover lands on the clock
    time it says.
    """
    if minutes is None:
        return None
    local = (datetime.combine(date.fromisoformat(day), datetime.min.time())
             + timedelta(minutes=int(minutes)))
    return local.replace(tzinfo=tz).astimezone(timezone.utc).isoformat()


def _annotate(rows: list[dict], defs: list[dict], day: str) -> list[dict]:
    """Copy each row with its definition's optional flag and deadline instant.

    Copies: the non-fresh path hands in rows from the shared history cache.
    """
    by_title = {d["title"]: d for d in defs}
    tz = _app_tz()
    out = []
    for r in rows:
        d = by_title.get(r["title"]) or {}
        out.append({
            **r,
            "optional": bool(d.get("optional")),
            "deadline_at": _deadline_at(day, d.get("deadline_minutes"), tz),
        })
    return out


def _recurring_defs() -> list[dict]:
    """Active recurrence definitions -- what is due -- in page order."""
    return [d for d in _definitions() if d.get("active")]


@app.get("/api/day/{day}")
def api_day(day: str, response: Response, fresh: bool = False) -> list[dict]:
    """A day's tasks, ordered as the check-in page shows them.

    Background prefetches use the cached history, but the day actually visible
    in the browser asks for ``fresh=true`` and performs one small day-scoped
    query. Vercel may route consecutive requests to different warm instances,
    whose process-local history caches cannot be kept coherent with each other.
    A live visible-day read prevents those caches from making completed tasks
    or measurements appear to move backwards after a refresh.
    """
    _check_day(day)
    defs = _definitions(fresh=fresh)
    position = {d["title"]: i for i, d in enumerate(defs)}
    if fresh:
        response.headers["Cache-Control"] = "no-store"
        rows = [_shape(r) for r in _day_rows(_sb(), day)]
    else:
        rows = [r for r in _task_history() if r["day"] == day]
    rows.sort(key=lambda r: (position.get(r["title"], len(position)), r["title"]))
    rows = _annotate(rows, defs, day)

    # Whether this day still needs materialising, so the page can skip the
    # ensure POST when it does not -- which is almost always. Free: the
    # definitions are process-cached and the rows are already in hand.
    #
    # A header rather than a field, because this endpoint returns a bare list
    # and several pages consume that shape. Emitted only when the answer is
    # known: with no definitions loaded the honest answer is "no idea", and a
    # missing header leaves the page doing what it does today.
    try:
        active = [d for d in defs if d.get("active")]
        if active:
            missing = _missing_recurrences(day, rows, active)
            response.headers["X-Day-Complete"] = "0" if missing else "1"
    except Exception as exc:  # noqa: BLE001
        # Never fail a day read over an optimisation. No header, no skip.
        log.warning("completeness check failed for %s (%s)", day, exc)

    return rows


class TaskOrderBody(BaseModel):
    titles: list[str]


@app.post("/api/task_order")
def api_task_order(body: TaskOrderBody) -> dict:
    """Save the order the Today page lists tasks in, measurements included.

    The page sends the titles in the order it shows them. Titles that are not
    active definitions are ignored -- an old day can show tasks that no longer
    run -- and any active one the page did not send keeps its relative place
    after the ones it did, so a partial list can never drop a task.

    This writes recurring_tasks.sort_order, the definitions' display order. It
    touches no history, so the write floor does not apply. Only rows whose
    position actually changed are written.
    """
    if len(set(body.titles)) != len(body.titles):
        raise HTTPException(400, "a task is listed twice")
    active = [d for d in _definitions(fresh=True) if d.get("active")]
    by_title = {d["title"]: d for d in active}
    if len(by_title) != len(active):
        # recurring_tasks is unique on (list_title, title), not title alone.
        raise HTTPException(409, "two active tasks share a title; order by title is ambiguous")
    listed = [title for title in body.titles if title in by_title]
    if not listed:
        raise HTTPException(400, "none of these titles is an active task")
    order = listed + [d["title"] for d in active if d["title"] not in set(listed)]

    client = _sb()
    changed = 0
    for position, title in enumerate(order, 1):
        d = by_title[title]
        if d.get("sort_order") != position:
            client.table("recurring_tasks").update({"sort_order": position}).eq(
                "id", d["id"]).execute()
            changed += 1
    _cache.pop("definitions", None)
    return {"order": order, "changed": changed}


class DefinitionSettings(BaseModel):
    optional: bool | None = None
    deadline_minutes: int | None = None


def _freeze_order(active: list[dict]) -> None:
    """Pin every active definition to its current position.

    The default order puts optional tasks last, so flipping one to optional
    would move it -- unless the order is already explicit. Writing the current
    positions first means the flip changes the colour and nothing else.
    """
    client = _sb()
    for position, d in enumerate(active, 1):
        if d.get("sort_order") != position:
            client.table("recurring_tasks").update({"sort_order": position}).eq(
                "id", d["id"]).execute()


@app.post("/api/definitions/{def_id}")
def api_definition_settings(def_id: int, body: DefinitionSettings) -> dict:
    """Set whether a task is optional, and its deadline. Definitions only, no history.

    deadline_minutes counts from midnight of the task's day; 1440-2879 is the
    following day. Send it as null to clear the deadline; leave it out to keep it.
    """
    sent = body.model_fields_set
    if not sent:
        raise HTTPException(400, "nothing to change")
    if "optional" in sent and body.optional is None:
        raise HTTPException(400, "optional must be true or false")
    if body.deadline_minutes is not None and not 0 <= body.deadline_minutes <= 2879:
        raise HTTPException(400, "deadline must be between 00:00 and 23:59 the next day")

    defs = _definitions(fresh=True)
    target = next((d for d in defs if d["id"] == def_id), None)
    if target is None:
        raise HTTPException(404, f"no task definition {def_id}")
    if "optional" not in target or "deadline_minutes" not in target:
        raise HTTPException(
            409, "run sql/migrations/2026-10-12_optional_and_deadline.sql first")

    if "optional" in sent and bool(target.get("optional")) != body.optional:
        active = [d for d in defs if d.get("active")]
        if any(not d.get("sort_order") for d in active):
            _freeze_order(active)

    changes = {k: getattr(body, k) for k in sent}
    updated = (_sb().table("recurring_tasks").update(changes)
               .eq("id", def_id).execute().data or [])
    _cache.pop("definitions", None)
    if not updated:
        raise HTTPException(404, f"no task definition {def_id}")
    row = updated[0]
    return {k: row.get(k) for k in ("id", "title", "optional", "deadline_minutes")}


@app.post("/api/task/{task_id}/completed_at")
def api_set_completed_at(task_id: int, at: str) -> dict:
    """Correct when a completion actually happened.

    The recorded time is when the box was ticked, which can be well after the
    fact. Deadlines judge by this time, so it can be set to when the task was
    really done. Only for completed rows, never earlier than the start of the
    task's day, never in the future, and never below the write floor.
    """
    try:
        when = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"not a timestamp: {at!r}")
    if when.tzinfo is None:
        raise HTTPException(400, "timestamp needs a UTC offset")

    client = _sb()
    found = (client.table("task_history").select("day,is_completed")
             .eq("id", task_id).execute().data)
    if not found:
        raise HTTPException(404, f"no task_history row with id {task_id}")
    day = str(found[0]["day"])[:10]
    _check_writable(day)
    if not found[0].get("is_completed"):
        raise HTTPException(400, "only a completed task has a completion time")

    start = datetime.combine(date.fromisoformat(day), datetime.min.time()).replace(tzinfo=_app_tz())
    if when < start:
        raise HTTPException(400, "a task cannot be completed before its day began")
    if when > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise HTTPException(400, "that time is in the future")

    res = (client.table("task_history")
           .update({"completed_at": when.astimezone(timezone.utc).isoformat()})
           .eq("id", task_id).execute())
    _cache_apply(res.data or [])
    return res.data[0] if res.data else {"id": task_id}


@app.post("/api/task/{task_id}/toggle")
def api_toggle(task_id: int, done: bool) -> dict:
    """Set a single task's completion state."""
    client = _sb()
    # Addressed by id, not day -- look the row up so the write floor still applies.
    found = client.table("task_history").select("day").eq("id", task_id).execute().data
    if not found:
        raise HTTPException(404, f"no task_history row with id {task_id}")
    _check_writable(str(found[0]["day"])[:10])

    res = client.table("task_history").update({
        "is_completed": done,
        "status": "completed" if done else "needsAction",
        "completed_at": datetime.now(timezone.utc).isoformat() if done else None,
    }).eq("id", task_id).execute()
    if not res.data:
        raise HTTPException(404, f"no task_history row with id {task_id}")
    _cache_apply(res.data)
    return res.data[0]


@app.post("/api/day/{day}/weight")
def api_set_weight(day: str, lbs: float) -> dict:
    """Record a weigh-in, writing both the numeric column and the note text."""
    _check_writable(day)
    if not (WEIGHT_MIN <= lbs <= WEIGHT_MAX):
        raise HTTPException(
            400, f"{lbs} lbs is outside {WEIGHT_MIN}-{WEIGHT_MAX}; refusing as a typo."
        )
    client = _sb()
    rows = [r for r in _day_rows(client, day) if r["title"] == "Weigh yourself"]
    if not rows:
        # The page materialises today's recurring rows asynchronously. A user
        # can enter a weight before that request finishes, so make the write
        # self-sufficient and retry the day-scoped lookup once.
        api_ensure_day(day)
        rows = [r for r in _day_rows(client, day) if r["title"] == "Weigh yourself"]
    if not rows:
        raise HTTPException(404, f"no 'Weigh yourself' row for {day} -- ensure the day first.")
    row = rows[0]
    previous_rows = (
        client.table("task_history")
        .select("day,weight_lbs")
        .eq("title", "Weigh yourself")
        .lt("day", day)
        .filter("weight_lbs", "not.is", "null")
        .order("day", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    warning = _weight_warning(lbs, previous_rows[0] if previous_rows else None)

    notes = row.get("notes") or "Start Date+Weight: 8/8/24, 181.9 lbs, Current Weight: "
    if CURRENT_WEIGHT_CLAUSE.search(notes):
        notes = CURRENT_WEIGHT_CLAUSE.sub(f"Current Weight: {lbs:.1f} lbs", notes, count=1)
    else:
        notes = notes.rstrip() + f" {lbs:.1f} lbs"

    res = client.table("task_history").update({
        "weight_lbs": lbs,
        "weight_source": "recorded",
        "notes": notes,
        "is_completed": True,
        "status": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", row["id"]).execute()
    _cache_apply(res.data)
    result = dict(res.data[0])
    result["warning"] = warning
    return result


@app.post("/api/day/{day}/wake")
def api_set_wake(day: str, time: str) -> dict:
    """Record a wake-up time as HH:MM (24h)."""
    _check_writable(day)
    m = TIME_RE.match(time or "")
    if not m:
        raise HTTPException(400, f"time must be HH:MM (24h), got {time!r}")
    hour, minute = int(m.group(1)), int(m.group(2))
    suffix = "am" if hour < 12 else "pm"
    display = f"{(hour % 12) or 12}:{minute:02d} {suffix}"

    client = _sb()
    rows = [r for r in _day_rows(client, day) if r["title"] == "Wake Up"]
    if not rows:
        raise HTTPException(404, f"no 'Wake Up' row for {day} -- ensure the day first.")
    row = rows[0]

    res = client.table("task_history").update({
        "notes": f"Wake-up Time: {display}",
        "is_completed": True,
        "status": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", row["id"]).execute()
    _cache_apply(res.data)
    return res.data[0]


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
@app.get("/today", response_class=HTMLResponse)
def page_today(day: str | None = None) -> str:
    """The daily check-in, and the app's front door.

    Served at both / and /today. Recording the day is the thing done every
    day; the calendar and charts are for looking back, so they sit one click
    away rather than in the way.
    """
    from zoneinfo import ZoneInfo

    from .timezones import resolve_timezone

    tz = ZoneInfo(resolve_timezone(load_config(), allow_google=False))
    today = datetime.now(tz).date().isoformat()
    if day:
        _check_day(day)
    else:
        day = today
    # A day below the write floor still renders -- read-only rather than broken.
    # `today` and `floor` travel with the page so the client can move between
    # days on its own: it needs the configured timezone's idea of today, not the
    # browser's, and it needs to decide writability without asking again.
    return _render("today.html", {
        "day": day,
        "today": today,
        "writable": date.fromisoformat(day) >= WRITE_FLOOR,
        "floor": WRITE_FLOOR.isoformat(),
    }, active="/")


@app.get("/calendar", response_class=HTMLResponse)
def page_calendar() -> str:
    return _render("calendar_view.html", {
        "mode": "live", "endpoint": "/api/task_history", "paged": False,
        "optional": sorted(d["title"] for d in _definitions() if d.get("optional")),
    }, active="/calendar")


@app.get("/tasks", response_class=HTMLResponse)
def page_tasks() -> str:
    """Per-task settings: optional or required, and a deadline."""
    defs = [d for d in _definitions(fresh=True) if d.get("active")]
    migrated = all("optional" in d and "deadline_minutes" in d for d in defs)
    return _render("tasks.html", {
        "mode": "inline",
        "migrated": migrated,
        "defs": [{"id": d["id"], "title": d["title"],
                  "optional": bool(d.get("optional")),
                  "deadline_minutes": d.get("deadline_minutes")} for d in defs],
    }, active="/tasks")


# Both series render through scripts/templates/series_chart.html. They differ
# only in labels, units and axis direction, so the differences live here rather
# than in a second copy of a 500-line template.
WEIGHT_CFG = {
    "title": "Weight Log",
    "eyebrow": "task_history \u00b7 weight",
    "sub": "Every weigh-in on record, read from the reconciled data.",
    "unit": "lbs",
    "format": "number",
    "invert": False,
    "legend": "Daily",
    "noun": "weigh-ins",
}

WAKE_CFG = {
    "title": "Wake-up Time",
    "eyebrow": "task_history \u00b7 wake up",
    "sub": "When the day started. Lower on the chart is earlier.",
    "unit": "",
    "format": "time",
    # No inversion. The default already places smaller values lower, which is
    # exactly "lower is earlier"; inverting put midnight at the top and
    # contradicted the caption.
    "invert": False,
    "legend": "Daily",
    "noun": "mornings",
    "statLabels": {
        "lbl-start": "First",
        "lbl-low": "Earliest",
        "lbl-high": "Latest riser",
    },
}


@app.get("/weight", response_class=HTMLResponse)
def page_weight() -> str:
    return _render("series_chart.html", {
        "mode": "live", "endpoint": "/api/weights", "cfg": WEIGHT_CFG,
    }, active="/weight")


@app.get("/wake", response_class=HTMLResponse)
def page_wake() -> str:
    return _render("series_chart.html", {
        "mode": "live", "endpoint": "/api/wake", "cfg": WAKE_CFG,
    }, active="/wake")


@app.get("/consistency", response_class=HTMLResponse)
def page_consistency() -> str:
    return _render("consistency.html", {
        "mode": "live", "endpoint": "/api/consistency",
    }, active="/consistency")


@app.get("/data", response_class=HTMLResponse)
def page_data() -> str:
    return _render("data_table.html", {
        "mode": "live", "endpoint": "/api/task_history",
    }, active="/data")


# --------------------------------------------------------------------------- #
# The pool -- undated work, drawn as a constellation
#
# Writes here are bounded to pool_tasks and pool_edges. They cannot reach
# task_history, and there is no WRITE_FLOOR because there are no dates to be
# before: a pool task has no day, which is the whole reason it exists.
# --------------------------------------------------------------------------- #
POOL_COLS = ("id", "title", "notes", "x", "y", "created_at", "resolved_at")


def _pool_patch(fn) -> None:
    """Apply fn to the cached pool state in place, keeping it fresh across writes."""
    hit = _cache.get("pool")
    if not hit:
        return
    ts, state = hit
    fn(state)
    _cache["pool"] = (ts, state)


def _pool_missing(exc: Exception) -> bool:
    """True when the failure is 'the tables are not there yet'.

    The schema has to be applied by hand in Supabase, so between merging this
    and running the SQL the page should say so rather than throw a stack trace.
    """
    text = str(exc).lower()
    return "pool_tasks" in text or "pool_edges" in text or "does not exist" in text


def _pool_fetch() -> dict:
    """Pull every star and every edge from Supabase. Always includes resolved."""
    client = _sb()
    tasks = (client.table("pool_tasks").select(",".join(POOL_COLS))
             .order("id").execute().data or [])
    for row in tasks:
        row["x"] = float(row.get("x") or 0.5)
        row["y"] = float(row.get("y") or 0.5)
    edges = (client.table("pool_edges")
             .select("before_id,after_id").execute().data or [])
    edges.sort(key=lambda e: (e["before_id"], e["after_id"]))
    return {"tasks": tasks, "edges": edges}


def _pool_all() -> dict:
    return _cached("pool", _pool_fetch, ttl=POOL_CACHE_TTL)


def _pool_state(include_resolved: bool = False) -> dict:
    """The stars and the edges between them.

    Resolved ones are left out by default -- a resolved star leaves the sky, and
    returning them always would grow the payload without end. They keep their
    row and their resolved_at, so `show resolved` can bring them back as embers
    and nothing is ever lost.
    """
    full = _pool_all()
    tasks = full["tasks"] if include_resolved else [
        t for t in full["tasks"] if not t.get("resolved_at")]
    present = {r["id"] for r in tasks}
    edges = [e for e in full["edges"]
             if e["before_id"] in present and e["after_id"] in present]
    return {"tasks": tasks, "edges": edges}


@app.get("/api/pool")
def api_pool(resolved: bool = False) -> dict:
    try:
        return _pool_state(include_resolved=resolved)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        if _pool_missing(exc):
            return {"tasks": [], "edges": [], "schema_missing": True}
        raise


@app.post("/api/pool/task/{task_id}/notes")
def api_pool_notes(task_id: int, text: str = "") -> dict:
    """The description behind a star. Empty clears it rather than storing ''."""
    if len(text) > 4000:
        raise HTTPException(400, "description longer than 4000 characters")
    updated = (
        _sb().table("pool_tasks").update({"notes": text.strip() or None})
        .eq("id", task_id).execute().data or []
    )
    if not updated:
        raise HTTPException(404, f"no pool task {task_id}")
    row = updated[0]
    def _patch(s):
        for t in s["tasks"]:
            if t["id"] == task_id:
                t["notes"] = row.get("notes")
                break
    _pool_patch(_patch)
    return row


def _clamp01(v: float) -> float:
    return 0.02 if v < 0.02 else (0.98 if v > 0.98 else float(v))


@app.post("/api/pool/task")
def api_pool_add(title: str, x: float = 0.5, y: float = 0.5, notes: str | None = None) -> dict:
    title = (title or "").strip()
    if not title:
        raise HTTPException(400, "a star needs a name")
    if len(title) > 200:
        raise HTTPException(400, "title longer than 200 characters")
    created = _sb().table("pool_tasks").insert({
        "title": title, "notes": notes,
        "x": _clamp01(x), "y": _clamp01(y),
    }).execute().data or []
    if not created:
        raise HTTPException(500, "insert returned nothing")
    row = created[0]
    row["x"] = float(row.get("x") or 0.5)
    row["y"] = float(row.get("y") or 0.5)
    _pool_patch(lambda s: s["tasks"].append(row))
    return row


@app.post("/api/pool/task/{task_id}/move")
def api_pool_move(task_id: int, x: float, y: float) -> dict:
    """Where a star sits. Clamped into the field so nothing can be dragged
    somewhere it cannot be dragged back from."""
    cx, cy = _clamp01(x), _clamp01(y)
    updated = (
        _sb().table("pool_tasks")
        .update({"x": cx, "y": cy})
        .eq("id", task_id).is_("resolved_at", "null").execute().data or []
    )
    if not updated:
        raise HTTPException(404, f"no open pool task {task_id}")
    def _patch(s):
        for t in s["tasks"]:
            if t["id"] == task_id:
                t["x"] = float(cx)
                t["y"] = float(cy)
                break
    _pool_patch(_patch)
    return updated[0]


@app.post("/api/pool/task/{task_id}/resolve")
def api_pool_resolve(task_id: int, done: bool = True) -> dict:
    """Resolving is allowed even while something it waits on is still open.

    The graph is advice. A hard lock would strand a task whenever a dependency
    turned out to be wrong, and would need an unlink-to-escape flow to undo.
    """
    stamp = datetime.now(timezone.utc).isoformat() if done else None
    updated = (
        _sb().table("pool_tasks").update({"resolved_at": stamp})
        .eq("id", task_id).execute().data or []
    )
    if not updated:
        raise HTTPException(404, f"no pool task {task_id}")
    row = updated[0]
    def _patch(s):
        for t in s["tasks"]:
            if t["id"] == task_id:
                t["resolved_at"] = row.get("resolved_at")
                break
    _pool_patch(_patch)
    return row


def _would_cycle(edges: list[dict], before: int, after: int) -> bool:
    """Does before -> after close a loop?

    True when `before` is already reachable from `after`. Two tasks each waiting
    on the other is not a constraint, it is a deadlock -- and since resolving is
    soft it would be a silent one, a pair of stars dimmed forever with nothing
    explaining why.
    """
    forward: dict[int, list[int]] = {}
    for e in edges:
        forward.setdefault(e["before_id"], []).append(e["after_id"])
    seen, stack = set(), [after]
    while stack:
        node = stack.pop()
        if node == before:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(forward.get(node, []))
    return False


@app.delete("/api/pool/task/{task_id}")
def api_pool_delete(task_id: int) -> dict:
    """Remove a star outright.

    Resolving is for work that happened; this is for a star that should never
    have existed -- a typo, a duplicate, something that stopped mattering. It is
    not recoverable, which is why the page puts a confirmation in front of it
    and keeps the control away from Resolve.

    Edges pointing at it go with it, by the cascade on pool_edges.
    """
    removed = (
        _sb().table("pool_tasks").delete().eq("id", task_id).execute().data or []
    )
    if not removed:
        raise HTTPException(404, f"no pool task {task_id}")
    def _patch(s):
        s["tasks"] = [t for t in s["tasks"] if t["id"] != task_id]
        s["edges"] = [e for e in s["edges"]
                      if e["before_id"] != task_id and e["after_id"] != task_id]
    _pool_patch(_patch)
    return {"id": task_id, "deleted": True, "title": removed[0].get("title")}


@app.post("/api/pool/link")
def api_pool_link(before_id: int, after_id: int) -> dict:
    if before_id == after_id:
        raise HTTPException(400, "a star cannot wait on itself")
    cached = _pool_all()
    edges = cached["edges"]
    if any(e["before_id"] == before_id and e["after_id"] == after_id for e in edges):
        return {"before_id": before_id, "after_id": after_id, "already": True}
    if _would_cycle(edges, before_id, after_id):
        raise HTTPException(400, "that link would close a loop")
    _sb().table("pool_edges").insert(
        {"before_id": before_id, "after_id": after_id}).execute()
    edge = {"before_id": before_id, "after_id": after_id}
    _pool_patch(lambda s: s["edges"].append(edge))
    return edge


@app.post("/api/pool/unlink")
def api_pool_unlink(before_id: int, after_id: int) -> dict:
    (
        _sb().table("pool_edges").delete()
        .eq("before_id", before_id).eq("after_id", after_id).execute()
    )
    def _patch(s):
        s["edges"] = [e for e in s["edges"]
                      if not (e["before_id"] == before_id and e["after_id"] == after_id)]
    _pool_patch(_patch)
    return {"before_id": before_id, "after_id": after_id, "removed": True}


@app.get("/pool", response_class=HTMLResponse)
def page_pool() -> str:
    try:
        state = _pool_state(include_resolved=True)
    except Exception as exc:  # noqa: BLE001
        if not _pool_missing(exc):
            raise
        state = {"tasks": [], "edges": [], "schema_missing": True}
    return _render("pool.html", {"mode": "inline", **state}, active="/pool")


@app.get("/health")
def health() -> dict:
    rows = _task_history()
    return {"ok": True, "task_history_rows": len(rows),
            "cached": "task_history" in _cache}
