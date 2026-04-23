"""
Google OAuth2 authentication helper.

On first run, opens a browser or prints a URL for the OAuth consent flow,
then saves the token to credentials/google_token.json.
Subsequent runs load and auto-refresh the saved token.
"""

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from google.auth.exceptions import RefreshError
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

_PROACTIVE_REFRESH_MINUTES = 5


class GoogleAuthService:
    def __init__(self, token_path: str, credentials_path: str, scopes: list[str]):
        self._token_path = Path(token_path)
        self._creds_path = Path(credentials_path)
        self._scopes = scopes
        self._creds: Credentials | None = None

    def get_credentials(self) -> Credentials:
        """Always returns valid, non-expired credentials. Refreshes if needed."""
        if self._creds is None and self._token_path.exists():
            self._creds = Credentials.from_authorized_user_file(str(self._token_path), self._scopes)

        if not self._creds:
            self._run_initial_auth_flow()
        elif not self._creds.valid:
            if self._creds.expired and self._creds.refresh_token:
                self._refresh_credentials()
            else:
                self._run_initial_auth_flow()
        elif self._creds.expiry and datetime.utcnow() + timedelta(minutes=_PROACTIVE_REFRESH_MINUTES) >= self._creds.expiry:
            logger.info("Google token expires in under %d minutes — refreshing proactively", _PROACTIVE_REFRESH_MINUTES)
            self._refresh_credentials()

        return self._creds

    def _refresh_credentials(self) -> None:
        logger.info("Refreshing Google OAuth token")
        try:
            self._creds.refresh(Request())
        except RefreshError:
            logger.warning("Google refresh token revoked or expired — starting OAuth flow")
            self._run_initial_auth_flow()
            return
        except Exception as exc:
            logger.info("Token refresh failed (%s), retrying in 3 seconds", exc)
            time.sleep(3)
            self._creds.refresh(Request())  # raises if still failing
        self._persist()

    def _run_initial_auth_flow(self) -> None:
        if not self._creds_path.exists():
            raise FileNotFoundError(
                f"OAuth credentials not found at {self._creds_path}.\n"
                "Download credentials.json from Google Cloud Console "
                "(APIs & Services → Credentials → OAuth 2.0 Client ID → Desktop app) "
                "and save it as credentials/google_oauth.json."
            )
        logger.info("Starting Google OAuth consent flow")
        flow = InstalledAppFlow.from_client_secrets_file(str(self._creds_path), self._scopes)
        self._creds = flow.run_local_server(port=0)
        self._persist()

    def _persist(self) -> None:
        self._token_path.write_text(self._creds.to_json())
        logger.info("Google token saved to %s", self._token_path)


# Module-level singleton — initialises lazily on first get_credentials() call
_service = GoogleAuthService(
    token_path=str(TOKEN_FILE),
    credentials_path=str(OAUTH_CREDS_FILE),
    scopes=SCOPES,
)


def get_credentials() -> Credentials:
    """Load, refresh, or obtain Google OAuth2 credentials.

    Always returns valid, non-expired credentials. Refreshes proactively
    when fewer than 5 minutes remain before expiry.

    Raises:
        FileNotFoundError: If google_oauth.json is missing.
    """
    return _service.get_credentials()
