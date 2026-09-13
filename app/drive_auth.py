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
import errno
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

# Google adds `openid` when email is requested; oauthlib otherwise aborts.
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.drive_scopes import DRIVE_SCOPE

HOST = "localhost"
DEFAULT_PORT = 8090
LIBRARY_FOLDER = "Telegram Music"
REVIEW_FOLDER = "Telegram Music Review"


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    google_client_id: str
    google_client_secret: str
    google_refresh_token: str = ""


def _redirect_uri(port: int) -> str:
    return f"http://{HOST}:{port}/"


def _client_config(settings: AuthSettings, redirect: str | None = None) -> dict:
    return {
        "installed": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect or _redirect_uri(DEFAULT_PORT)],
        }
    }


def _is_addr_in_use(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno in {
        errno.EADDRINUSE,
        getattr(errno, "WSAEADDRINUSE", 10048),
    }


def _can_bind(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
        return True
    except OSError:
        return False


def _proc_name(pid: int) -> str:
    try:
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
        if comm:
            return comm
    except OSError:
        pass
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode().strip()
        if cmdline:
            return cmdline.split()[0]
    except OSError:
        pass
    return "?"


def _run_capture(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    return (proc.stdout or "") + (proc.stderr or "")


def _listeners_on_port(port: int) -> list[tuple[int, str]]:
    pids: dict[int, str] = {}

    lsof = _run_capture(["lsof", f"-iTCP:{port}", "-sTCP:LISTEN", "-n", "-P", "-F", "pc"])
    cur: int | None = None
    for line in lsof.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            cur = int(line[1:])
            pids.setdefault(cur, "?")
        elif line.startswith("c") and cur is not None:
            pids[cur] = line[1:] or pids[cur]

    if not pids:
        ss = _run_capture(["ss", "-ltnp", f"sport = :{port}"])
        for match in re.finditer(r'pid=(\d+)', ss):
            pids.setdefault(int(match.group(1)), "?")
        for match in re.finditer(r'\(\("([^"]+)",pid=(\d+)', ss):
            pids[int(match.group(2))] = match.group(1)

    if not pids:
        fuser = _run_capture(["fuser", f"{port}/tcp"])
        for match in re.finditer(r"\d+", fuser):
            pids.setdefault(int(match.group()), "?")

    for pid in list(pids):
        if pids[pid] in {"", "?"}:
            pids[pid] = _proc_name(pid)
    return sorted(pids.items())


def _format_listeners(listeners: list[tuple[int, str]]) -> str:
    if not listeners:
        return "no listening process (TIME_WAIT or permission)"
    return ", ".join(f"pid {pid} ({name})" for pid, name in listeners)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _kill_pids(pids: list[int], *, sigterm_wait: float = 2.0) -> None:
    mine = os.getpid()
    targets = [pid for pid in pids if pid > 0 and pid != mine]
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"Sent SIGTERM to pid {pid}.", file=sys.stderr)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            print(f"Cannot kill pid {pid}: {exc}", file=sys.stderr)
    deadline = time.monotonic() + sigterm_wait
    alive = set(targets)
    while alive and time.monotonic() < deadline:
        alive = {pid for pid in alive if _pid_alive(pid)}
        if alive:
            time.sleep(0.05)
    for pid in list(alive):
        try:
            os.kill(pid, signal.SIGKILL)
            print(f"Sent SIGKILL to pid {pid}.", file=sys.stderr)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            print(f"Cannot SIGKILL pid {pid}: {exc}", file=sys.stderr)


def _wait_can_bind(host: str, port: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _can_bind(host, port):
            return True
        time.sleep(0.05)
    return _can_bind(host, port)


def _next_free_port(host: str, start: int) -> int:
    begin = min(max(start, 1), 65535)
    for port in range(begin, min(begin + 200, 65536)):
        if _can_bind(host, port):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _prompt_busy_choice(has_listeners: bool) -> str:
    print("  [k] Kill process and reuse this port" if has_listeners else "  [k] Kill (no process found)", file=sys.stderr)
    print("  [n] Pick next free port", file=sys.stderr)
    print("  [q] Quit", file=sys.stderr)
    return input("Choice [k/n/q]: ").strip().lower()


def _resolve_bind_port(host: str, preferred: int) -> int:
    if _can_bind(host, preferred):
        return preferred

    listeners = _listeners_on_port(preferred)
    print(f"{host}:{preferred} already in use ({_format_listeners(listeners)}).", file=sys.stderr)

    if not sys.stdin.isatty():
        nxt = _next_free_port(host, preferred + 1)
        print(f"stdin is not a TTY; using {host}:{nxt}.", file=sys.stderr)
        return nxt

    while True:
        choice = _prompt_busy_choice(bool(listeners))
        if choice in {"q", "quit"}:
            raise SystemExit(1)
        if choice in {"n", "new"}:
            nxt = _next_free_port(host, preferred + 1)
            print(f"Using {host}:{nxt}.", file=sys.stderr)
            return nxt
        if choice in {"k", "kill", "y", "yes"}:
            pids = [pid for pid, _ in listeners]
            if not pids:
                print("Nothing to kill. Pick [n] or [q].", file=sys.stderr)
                continue
            _kill_pids(pids)
            if _wait_can_bind(host, preferred):
                print(f"Reusing {host}:{preferred}.", file=sys.stderr)
                return preferred
            print(f"Still cannot bind {host}:{preferred}. Pick another option.", file=sys.stderr)
            listeners = _listeners_on_port(preferred)
            print(f"Still in use ({_format_listeners(listeners)}).", file=sys.stderr)
            continue
        print("Type k, n, or q.", file=sys.stderr)


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


def run_browser(settings: AuthSettings, port: int = DEFAULT_PORT) -> int:
    preferred = port
    while True:
        bound = _resolve_bind_port(HOST, preferred)
        flow = InstalledAppFlow.from_client_config(
            _client_config(settings, _redirect_uri(bound)), DRIVE_SCOPE
        )
        print("Sign in with the Google account that should own the music files.")
        print("Allow drive.file + email when asked.")
        print(f"OAuth callback: {_redirect_uri(bound)}")
        try:
            creds = flow.run_local_server(
                host=HOST,
                port=bound,
                access_type="offline",
                prompt="consent",
            )
            break
        except OSError as exc:
            if not _is_addr_in_use(exc):
                raise
            print(f"Bind failed on {HOST}:{bound}: {exc}", file=sys.stderr)
            preferred = bound
    library_id, review_id = _make_folders(creds)
    return _print_env(creds.refresh_token, library_id, review_id)


def run_manual(settings: AuthSettings, port: int = DEFAULT_PORT) -> int:
    redirect = _redirect_uri(port)
    flow = InstalledAppFlow.from_client_config(_client_config(settings, redirect), DRIVE_SCOPE)
    flow.redirect_uri = redirect
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    print("1. Open this URL (account that should own the files):\n")
    print(auth_url)
    print(
        f"\n2. After allow, browser hits localhost:{port} and may fail to load.\n"
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
        refresh_token=settings.google_refresh_token.strip(),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id.strip(),
        client_secret=settings.google_client_secret.strip(),
        scopes=DRIVE_SCOPE,
    )
    try:
        creds.refresh(Request())
    except RefreshError:
        print(
            "Google OAuth refresh token is invalid or expired (invalid_grant). "
            "Re-run `python -m app.drive_auth` (without --setup-folders) "
            "and update GOOGLE_REFRESH_TOKEN.",
            file=sys.stderr,
        )
        return 1
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
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"OAuth localhost callback port (default {DEFAULT_PORT})",
    )
    args = parser.parse_args()
    settings = AuthSettings()
    if args.setup_folders:
        return run_setup_folders(settings)
    if args.manual:
        return run_manual(settings, port=args.port)
    return run_browser(settings, port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
