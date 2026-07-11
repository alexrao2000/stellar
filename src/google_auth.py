"""Google OAuth handling with cached desktop-app token."""
from __future__ import annotations

import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import TASKS_SCOPES, Config


def get_credentials(config: Config) -> Credentials:
    """Return valid OAuth credentials, refreshing or running the consent flow as needed."""
    creds: Credentials | None = None

    if os.path.exists(config.google_token_file):
        creds = Credentials.from_authorized_user_file(config.google_token_file, TASKS_SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file(config.google_credentials_file, TASKS_SCOPES)
        # Opens a browser for the one-time consent; falls back to console if no browser.
        creds = flow.run_local_server(port=0)

    with open(config.google_token_file, "w", encoding="utf-8") as token:
        token.write(creds.to_json())

    return creds
