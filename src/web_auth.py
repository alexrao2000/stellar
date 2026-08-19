"""Single-user Supabase Auth sessions for the web app.

The database secret never participates in authentication. Login, refresh and
token verification use only the publishable key; application data is read by
the server after the caller has been verified and allow-listed.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass

import httpx


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class AuthUser:
    id: str
    email: str


@dataclass(frozen=True)
class AuthSession:
    access_token: str
    refresh_token: str
    expires_in: int
    user: AuthUser


def _jwt_exp(token: str) -> int:
    """Read expiry only after Supabase has verified the token."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return int(json.loads(base64.urlsafe_b64decode(part))["exp"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return 0


class SupabaseWebAuth:
    def __init__(self) -> None:
        self._verified: dict[str, tuple[int, AuthUser]] = {}
        self._refresh_lock = asyncio.Lock()
        self._recent_refresh: dict[str, tuple[float, AuthSession]] = {}

    @staticmethod
    def _headers(key: str, access_token: str | None = None) -> dict[str, str]:
        headers = {"apikey": key, "Content-Type": "application/json"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    @staticmethod
    def _user(raw: dict) -> AuthUser:
        user_id = str(raw.get("id") or "")
        email = str(raw.get("email") or "").strip().casefold()
        if not user_id or not email:
            raise AuthError("Supabase returned an incomplete user")
        return AuthUser(user_id, email)

    def _session(self, raw: dict) -> AuthSession:
        access = str(raw.get("access_token") or "")
        refresh = str(raw.get("refresh_token") or "")
        if not access or not refresh:
            raise AuthError("Supabase returned an incomplete session")
        user = self._user(raw.get("user") or {})
        expires = max(60, int(raw.get("expires_in") or 3600))
        self._verified[access] = (_jwt_exp(access), user)
        return AuthSession(access, refresh, expires, user)

    async def _post(self, url: str, key: str, payload: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                response = await client.post(url, headers=self._headers(key), json=payload)
        except httpx.HTTPError as exc:
            raise AuthError("Authentication service is unavailable", 503) from exc
        if response.status_code >= 400:
            try:
                body = response.json()
                message = body.get("msg") or body.get("error_description") or body.get("message")
            except (ValueError, AttributeError):
                message = None
            raise AuthError(str(message or "Sign-in failed"), 401)
        return response.json()

    async def sign_in(self, url: str, key: str, email: str, password: str) -> AuthSession:
        raw = await self._post(
            f"{url.rstrip('/')}/auth/v1/token?grant_type=password",
            key,
            {"email": email.strip(), "password": password},
        )
        return self._session(raw)

    async def refresh(self, url: str, key: str, refresh_token: str) -> AuthSession:
        # Refresh tokens rotate. Coalesce requests that arrive together so one
        # browser burst cannot try to spend the same token more than once.
        async with self._refresh_lock:
            recent = self._recent_refresh.get(refresh_token)
            if recent and time.monotonic() - recent[0] < 15:
                return recent[1]
            raw = await self._post(
                f"{url.rstrip('/')}/auth/v1/token?grant_type=refresh_token",
                key,
                {"refresh_token": refresh_token},
            )
            session = self._session(raw)
            self._recent_refresh = {refresh_token: (time.monotonic(), session)}
            return session

    async def verify(self, url: str, key: str, access_token: str) -> AuthUser:
        cached = self._verified.get(access_token)
        if cached and cached[0] > int(time.time()) + 5:
            return cached[1]
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                response = await client.get(
                    f"{url.rstrip('/')}/auth/v1/user",
                    headers=self._headers(key, access_token),
                )
        except httpx.HTTPError as exc:
            raise AuthError("Authentication service is unavailable", 503) from exc
        if response.status_code >= 400:
            raise AuthError("Session expired")
        user = self._user(response.json())
        self._verified[access_token] = (_jwt_exp(access_token), user)
        return user


web_auth = SupabaseWebAuth()
