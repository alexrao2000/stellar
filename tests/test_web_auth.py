"""Authentication boundary tests; no live Supabase calls."""
from fastapi.testclient import TestClient

from src.server import ACCESS_COOKIE, REFRESH_COOKIE, app
from src.web_auth import AuthSession, AuthUser, web_auth


def _enable(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_ALLOWED_EMAIL", "alex@example.com")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")


def test_page_redirects_to_login_without_session(monkeypatch):
    _enable(monkeypatch)
    client = TestClient(app, follow_redirects=False)
    response = client.get("/calendar?year=2026")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fcalendar%3Fyear%3D2026"


def test_login_sets_http_only_rotating_session_cookies(monkeypatch):
    _enable(monkeypatch)

    async def sign_in(url, key, email, password):
        assert email == "alex@example.com"
        assert password == "correct horse"
        return AuthSession("access-one", "refresh-one", 3600,
                           AuthUser("user-1", "alex@example.com"))

    monkeypatch.setattr(web_auth, "sign_in", sign_in)
    client = TestClient(app, follow_redirects=False)
    response = client.post(
        "/auth/login",
        headers={"Origin": "http://testserver"},
        json={"email": "alex@example.com", "password": "correct horse"},
    )
    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    assert any(f"{ACCESS_COOKIE}=access-one" in c and "HttpOnly" in c for c in cookies)
    assert any(f"{REFRESH_COOKIE}=refresh-one" in c and "HttpOnly" in c for c in cookies)


def test_refresh_cookie_is_rotated_automatically(monkeypatch):
    _enable(monkeypatch)

    async def refresh(url, key, token):
        assert token == "refresh-old"
        return AuthSession("access-new", "refresh-new", 3600,
                           AuthUser("user-1", "alex@example.com"))

    monkeypatch.setattr(web_auth, "refresh", refresh)
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(REFRESH_COOKIE, "refresh-old")
    # A missing route is useful here: middleware completes authentication and
    # rotates cookies, then the router safely returns 404 without touching data.
    response = client.get("/does-not-exist")
    assert response.status_code == 404
    cookies = response.headers.get_list("set-cookie")
    assert any(f"{ACCESS_COOKIE}=access-new" in c for c in cookies)
    assert any(f"{REFRESH_COOKIE}=refresh-new" in c for c in cookies)


def test_cross_origin_write_is_refused(monkeypatch):
    _enable(monkeypatch)
    client = TestClient(app, follow_redirects=False)
    response = client.post(
        "/auth/login",
        headers={"Origin": "https://attacker.example"},
        json={"email": "alex@example.com", "password": "anything"},
    )
    assert response.status_code == 403
