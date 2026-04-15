"""
Google OAuth2 authentication helper.

On first run, opens a browser or prints a URL for the OAuth consent flow,
then saves the token to credentials/google_token.json.
Subsequent runs load and auto-refresh the saved token.
"""

import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.send",
]

_DIR = Path(__file__).parent
OAUTH_CREDS_FILE = _DIR / "google_oauth.json"
TOKEN_FILE = _DIR / "google_token.json"


def get_credentials() -> Credentials:
    """Load, refresh, or obtain Google OAuth2 credentials.

    - If a saved token exists and is valid, returns it immediately.
    - If the token is expired but has a refresh token, refreshes it.
    - If no token exists, runs the OAuth consent flow:
      - Tries a local redirect server (works when a browser is available).
      - Falls back to console/URL-print mode for headless Raspberry Pi.
    - Saves the token to TOKEN_FILE after any new auth or refresh.

    Raises:
        FileNotFoundError: If google_oauth.json is missing.
    """
    if not OAUTH_CREDS_FILE.exists():
        raise FileNotFoundError(
            f"OAuth credentials not found at {OAUTH_CREDS_FILE}.\n"
            "Download credentials.json from Google Cloud Console "
            "(APIs & Services → Credentials → OAuth 2.0 Client ID → Desktop app) "
            "and save it as credentials/google_oauth.json."
        )

    creds: Credentials | None = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing expired Google token")
            creds.refresh(Request())
        else:
            logger.info("Starting Google OAuth consent flow")
            flow = InstalledAppFlow.from_client_secrets_file(str(OAUTH_CREDS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(creds.to_json())
        logger.info("Google token saved to %s", TOKEN_FILE)

    return creds
