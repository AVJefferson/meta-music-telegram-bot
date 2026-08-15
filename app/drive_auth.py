"""One-time OAuth for personal My Drive using drive.file (no Google verification).

Consent screen scopes to enable:
  - .../auth/drive.file
  - .../auth/userinfo.email

drive.file only sees folders THIS APP created. This script creates
"Telegram Music" and "Telegram Music Review" and prints their IDs.
Move those folders into your Music folder in the Drive UI if you want.

Usage:
  python -m app.drive_auth
  python -m app.drive_auth --manual
  python -m app.drive_auth --setup-folders   # reuse existing refresh token
"""

from __future__ import annotations

import argparse
import os
import sys

# Google adds `openid` when email is requested; oauthlib otherwise aborts.
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.drive_scopes import DRIVE_SCOPE

REDIRECT = "http://localhost:8090/"
LIBRARY_FOLDER = "Telegram Music"
REVIEW_FOLDER = "Telegram Music Review"


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    google_client_id: str
    google_client_secret: str
    google_refresh_token: str = ""


def _client_config(settings: AuthSettings) -> dict:
    return {
        "installed": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT],
        }
    }


def _print_env(refresh_token: str | None, library_id: str | None, review_id: str | None) -> int:
    if not refresh_token:
        print(
            "No refresh_token returned. Remove this app at "
            "https://myaccount.google.com/permissions then retry.",
            file=sys.stderr,
        )
        return 1
    print("\nPaste into .env:\n")
    print(f"GOOGLE_REFRESH_TOKEN={refresh_token}")
    if library_id and review_id:
        print(f"GDRIVE_FOLDER_ID={library_id}")
        print(f"GDRIVE_REVIEW_FOLDER_ID={review_id}")
        print(
            "\nThose folders were created by this app (required for drive.file). "
            "You can move them into your existing Music folder in Drive; the bot keeps access."
        )
    else:
        print(
            "\nToken OK. Folders not created (google-api-python-client missing).\n"
            "  pip install google-api-python-client\n"
            "  python -m app.drive_auth --setup-folders"
        )
    return 0


def _make_folders(creds: Credentials) -> tuple[str | None, str | None]:
    try:
        from app.drive import DriveClient
    except ImportError:
        print(
            "google-api-python-client not installed; skipping folder create.",
            file=sys.stderr,
        )
        return None, None
    client = DriveClient(creds, email="oauth-setup")
    library_id, _ = client.ensure_named_folder(LIBRARY_FOLDER)
    review_id, _ = client.ensure_named_folder(REVIEW_FOLDER)
    print(f"Library folder: {LIBRARY_FOLDER}  id={library_id}")
    print(f"Review folder:  {REVIEW_FOLDER}  id={review_id}")
    return library_id, review_id


def run_browser(settings: AuthSettings) -> int:
    flow = InstalledAppFlow.from_client_config(_client_config(settings), DRIVE_SCOPE)
    print("Sign in with the Google account that should own the music files.")
    print("Allow drive.file + email when asked.")
    creds = flow.run_local_server(
        host="localhost",
        port=8090,
        access_type="offline",
        prompt="consent",
    )
    library_id, review_id = _make_folders(creds)
    return _print_env(creds.refresh_token, library_id, review_id)


def run_manual(settings: AuthSettings) -> int:
    flow = InstalledAppFlow.from_client_config(_client_config(settings), DRIVE_SCOPE)
    flow.redirect_uri = REDIRECT
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    print("1. Open this URL (account that should own the files):\n")
    print(auth_url)
    print(
        "\n2. After allow, browser hits localhost:8090 and may fail to load.\n"
        "   Copy the `code` query parameter from the address bar.\n"
    )
    code = input("3. Paste code: ").strip()
    flow.fetch_token(code=code)
    creds = flow.credentials
    library_id, review_id = _make_folders(creds)
    return _print_env(creds.refresh_token, library_id, review_id)


def run_setup_folders(settings: AuthSettings) -> int:
    if not settings.google_refresh_token:
        print("GOOGLE_REFRESH_TOKEN missing in .env", file=sys.stderr)
        return 1
    creds = Credentials(
        token=None,
        refresh_token=settings.google_refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=DRIVE_SCOPE,
    )
    creds.refresh(Request())
    library_id, review_id = _make_folders(creds)
    if not library_id or not review_id:
        return 1
    print("\nPaste into .env:\n")
    print(f"GDRIVE_FOLDER_ID={library_id}")
    print(f"GDRIVE_REVIEW_FOLDER_ID={review_id}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="OAuth + Drive folders for personal My Drive")
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Print a URL and paste the code (headless / remote)",
    )
    parser.add_argument(
        "--setup-folders",
        action="store_true",
        help="Create/reuse app folders using existing GOOGLE_REFRESH_TOKEN",
    )
    args = parser.parse_args()
    settings = AuthSettings()
    if args.setup_folders:
        return run_setup_folders(settings)
    if args.manual:
        return run_manual(settings)
    return run_browser(settings)


if __name__ == "__main__":
    raise SystemExit(main())
