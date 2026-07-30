"""Google OAuth handling with cached desktop-app token."""
from __future__ import annotations

import logging
import os

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import SCOPES, Config

log = logging.getLogger("cal-task-sync")


def _run_consent_flow(config: Config, scopes: list[str] | None = None) -> Credentials:
    """Open the browser consent flow and return fresh credentials."""
    log.info("Opening Google auth in your browser. Sign in and grant the requested access.")
    flow = InstalledAppFlow.from_client_secrets_file(
        config.google_credentials_file, scopes or SCOPES
    )
    return flow.run_local_server(port=0, open_browser=True)


def _save_token(config: Config, creds: Credentials, token_file: str | None = None) -> None:
    with open(token_file or config.google_token_file, "w", encoding="utf-8") as token:
        token.write(creds.to_json())


def _clear_token(config: Config, token_file: str | None = None) -> None:
    path = token_file or config.google_token_file
    if os.path.exists(path):
        os.remove(path)
        log.info("Removed stale token at %s", path)


def _materialize_inline_secrets(config: Config) -> None:
    """Write inline JSON secrets (from env vars) to their file paths for CI use."""
    if config.google_credentials_json and not os.path.exists(config.google_credentials_file):
        with open(config.google_credentials_file, "w", encoding="utf-8") as f:
            f.write(config.google_credentials_json)
    if config.google_token_json and not os.path.exists(config.google_token_file):
        with open(config.google_token_file, "w", encoding="utf-8") as f:
            f.write(config.google_token_json)


def get_credentials(
    config: Config,
    scopes: list[str] | None = None,
    token_file: str | None = None,
) -> Credentials:
    """Return valid OAuth credentials.

    - Materializes inline JSON secrets (CI) to files if provided.
    - Uses the cached token when still valid.
    - Refreshes the access token when expired (and saves the refreshed token).
    - If refresh fails (revoked / expired refresh token), deletes the cache and
      opens the browser consent link again (only possible in an interactive env).

    `scopes` and `token_file` default to the read-only sync credentials. The
    repair tooling passes TASKS_RW_SCOPES with its own token path so that
    consenting to writes never upgrades the token the sync and CI use.
    """
    _materialize_inline_secrets(config)

    scopes = scopes or SCOPES
    token_file = token_file or config.google_token_file
    creds: Credentials | None = None

    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, scopes)

    # If the cached token lacks a scope we now need, force a fresh consent.
    if creds and not creds.has_scopes(scopes):
        log.info("Cached token is missing required scopes; re-authenticating...")
        _clear_token(config, token_file)
        creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            log.info("Refreshing Google access token...")
            creds.refresh(Request())
            _save_token(config, creds, token_file)
            log.info("Access token refreshed.")
            return creds
        except RefreshError as exc:
            log.warning("Token refresh failed (%s). Re-authenticating...", exc)
            _clear_token(config, token_file)
            creds = None

    # No usable token (missing, no refresh_token, or refresh failed).
    if os.getenv("CI") or os.getenv("HEADLESS"):
        raise RuntimeError(
            f"No valid Google token at {token_file} for scopes {scopes}, and running "
            "headless so the browser consent flow cannot open. Run this command again "
            "in an interactive terminal to consent. For the read-only sync token that "
            "means 'make sync' (then update the GOOGLE_TOKEN_JSON secret for CI); "
            "publish the OAuth app to stop the weekly refresh-token expiry."
        )
    creds = _run_consent_flow(config, scopes)
    _save_token(config, creds, token_file)
    log.info("Google auth complete; token saved to %s", token_file)
    return creds


def reauth(config: Config) -> Credentials:
    """Force a fresh consent flow (deletes any cached token first)."""
    _clear_token(config)
    creds = _run_consent_flow(config)
    _save_token(config, creds)
    return creds
