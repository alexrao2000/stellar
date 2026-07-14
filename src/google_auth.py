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


def _run_consent_flow(config: Config) -> Credentials:
    """Open the browser consent flow and return fresh credentials."""
    log.info("Opening Google auth in your browser. Sign in and allow Tasks + Calendar access.")
    flow = InstalledAppFlow.from_client_secrets_file(config.google_credentials_file, SCOPES)
    return flow.run_local_server(port=0, open_browser=True)


def _save_token(config: Config, creds: Credentials) -> None:
    with open(config.google_token_file, "w", encoding="utf-8") as token:
        token.write(creds.to_json())


def _clear_token(config: Config) -> None:
    if os.path.exists(config.google_token_file):
        os.remove(config.google_token_file)
        log.info("Removed stale token at %s", config.google_token_file)


def _materialize_inline_secrets(config: Config) -> None:
    """Write inline JSON secrets (from env vars) to their file paths for CI use."""
    if config.google_credentials_json and not os.path.exists(config.google_credentials_file):
        with open(config.google_credentials_file, "w", encoding="utf-8") as f:
            f.write(config.google_credentials_json)
    if config.google_token_json and not os.path.exists(config.google_token_file):
        with open(config.google_token_file, "w", encoding="utf-8") as f:
            f.write(config.google_token_json)


def get_credentials(config: Config) -> Credentials:
    """Return valid OAuth credentials.

    - Materializes inline JSON secrets (CI) to files if provided.
    - Uses cached token.json when still valid.
    - Refreshes the access token when expired (and saves the refreshed token).
    - If refresh fails (revoked / expired refresh token), deletes the cache and
      opens the browser consent link again (only possible in an interactive env).
    """
    _materialize_inline_secrets(config)

    creds: Credentials | None = None

    if os.path.exists(config.google_token_file):
        creds = Credentials.from_authorized_user_file(config.google_token_file, SCOPES)

    # If the cached token lacks a scope we now need, force a fresh consent.
    if creds and not creds.has_scopes(SCOPES):
        log.info("Cached token is missing required scopes; re-authenticating...")
        _clear_token(config)
        creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            log.info("Refreshing Google access token...")
            creds.refresh(Request())
            _save_token(config, creds)
            log.info("Access token refreshed.")
            return creds
        except RefreshError as exc:
            log.warning("Token refresh failed (%s). Re-authenticating...", exc)
            _clear_token(config)
            creds = None

    # No usable token (missing, no refresh_token, or refresh failed).
    if os.getenv("CI") or os.getenv("HEADLESS"):
        raise RuntimeError(
            "No valid Google token and running headless (CI). The refresh token is "
            "likely expired/revoked. Re-run 'make sync' locally to re-consent, then "
            "update the GOOGLE_TOKEN_JSON secret. (Publish the OAuth app to stop weekly "
            "expiry.)"
        )
    creds = _run_consent_flow(config)
    _save_token(config, creds)
    log.info("Google auth complete; token saved to %s", config.google_token_file)
    return creds


def reauth(config: Config) -> Credentials:
    """Force a fresh consent flow (deletes any cached token first)."""
    _clear_token(config)
    creds = _run_consent_flow(config)
    _save_token(config, creds)
    return creds
