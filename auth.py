"""
auth.py — Google OAuth2 browser-based authentication.


First run: opens a browser window for sign-in, then saves token.json.
Subsequent runs: loads token.json and refreshes silently when expired.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from google.auth.exceptions import TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

_GCP_HELP = (
    "\nTo create credentials.json:\n"
    "  1. Go to https://console.cloud.google.com\n"
    "  2. Enable the Google Sheets API for your project\n"
    "  3. APIs & Services → Credentials → Create Credentials → OAuth client ID\n"
    "  4. Application type: Desktop app\n"
    "  5. Download the JSON file and rename it to 'credentials.json'\n"
    "  6. Place it in the project root directory\n"
)


def get_credentials(
    credentials_file: str = "credentials.json",
    token_file: str = "token.json",
) -> Credentials:
    """
    Return valid Google OAuth2 credentials.

    - If token_file exists and is valid: return immediately (no browser).
    - If token is expired but refreshable: refresh silently.
    - Otherwise: launch browser for interactive sign-in, then cache token.

    Args:
        credentials_file: Path to the OAuth2 client secrets JSON (from GCP).
        token_file: Path where the access/refresh token is cached.

    Raises:
        FileNotFoundError: If credentials_file does not exist.
        TransportError: On network failures during token refresh.
    """
    creds: Credentials | None = None

    # --- Load cached token ---
    token_path = Path(token_file)
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            logger.debug("Loaded cached credentials from %s", token_file)
        except Exception as exc:
            logger.warning("Could not load %s (%s); will re-authenticate.", token_file, exc)
            creds = None

    # --- Refresh or re-authenticate ---
    if creds and creds.valid:
        logger.debug("Credentials are valid; skipping authentication.")
        return creds

    if creds and creds.expired and creds.refresh_token:
        logger.info("Access token expired; refreshing…")
        try:
            creds.refresh(Request())
            logger.info("Token refreshed successfully.")
        except TransportError as exc:
            logger.error("Network error during token refresh: %s", exc)
            raise TransportError(
                "Could not refresh the access token. Check your internet connection."
            ) from exc
        except Exception as exc:
            logger.warning(
                "Token refresh failed (%s); deleting %s and re-authenticating.",
                exc, token_file,
            )
            token_path.unlink(missing_ok=True)
            creds = None

    if creds is None or not creds.valid:
        # --- Browser-based OAuth flow ---
        creds_path = Path(credentials_file)
        if not creds_path.exists():
            raise FileNotFoundError(
                f"credentials.json not found at '{credentials_file}'." + _GCP_HELP
            )

        logger.info(
            "Opening browser for Google sign-in… "
            "(approve the read-only Sheets access request)"
        )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(creds_path), SCOPES
        )
        # port=0 lets the OS pick a free port; opens browser automatically
        creds = flow.run_local_server(port=0)
        logger.info("Authentication successful.")

    # --- Persist token ---
    _save_token(creds, token_file)
    return creds


def _save_token(creds: Credentials, token_file: str) -> None:
    """Write credentials to token_file as JSON."""
    try:
        token_data = {
            "token":         creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri":     creds.token_uri,
            "client_id":     creds.client_id,
            "client_secret": creds.client_secret,
            "scopes":        list(creds.scopes) if creds.scopes else [],
        }
        with open(token_file, "w") as fh:
            json.dump(token_data, fh, indent=2)
        logger.debug("Token saved to %s", token_file)
    except OSError as exc:
        logger.warning("Could not save token to %s: %s", token_file, exc)
