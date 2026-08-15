from __future__ import annotations

import logging
import time
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from app.config import Settings
from app.drive_scopes import DRIVE_SCOPE

log = logging.getLogger(__name__)
FOLDER_MIME = "application/vnd.google-apps.folder"
_NO_RETRY_STATUS = {400, 401, 403, 404}


class DriveAccessError(RuntimeError):
    """Folder missing, wrong auth, or Drive rejected the upload."""


def _oauth_email(creds: Credentials) -> str:
    try:
        info = build("oauth2", "v2", credentials=creds, cache_discovery=False).userinfo().get().execute()
        return str(info.get("email") or "oauth-user")
    except Exception:
        log.debug("oauth2 userinfo failed", exc_info=True)
        return "oauth-user"


def _escape_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _quota_error(exc: HttpError) -> bool:
    text = str(exc)
    return exc.resp.status == 403 and (
        "storageQuotaExceeded" in text or "Service Accounts do not have storage quota" in text
    )


class DriveClient:
    def __init__(self, creds, *, email: str) -> None:
        self.email = email
        self._creds = creds
        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        self._folder_cache: dict[tuple[str, str], str] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> DriveClient:
        if (
            settings.google_refresh_token
            and settings.google_client_id
            and settings.google_client_secret
        ):
            creds = Credentials(
                token=None,
                refresh_token=settings.google_refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=settings.google_client_id,
                client_secret=settings.google_client_secret,
                scopes=DRIVE_SCOPE,
            )
            creds.refresh(Request())
            email = _oauth_email(creds)
            log.info("drive auth=oauth drive.file user=%s", email)
            return cls(creds, email=email)
        if settings.google_service_account_json:
            raise DriveAccessError(
                "Service accounts cannot upload to personal My Drive (0 quota). "
                "Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN "
                "and run: python -m app.drive_auth"
            )
        raise DriveAccessError(
            "No Drive credentials. Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, "
            "GOOGLE_REFRESH_TOKEN (python -m app.drive_auth)."
        )

    def _wrap_http_error(self, exc: HttpError, folder_id: str | None = None) -> Exception:
        if _quota_error(exc):
            return DriveAccessError(
                "Service accounts have 0 My Drive quota. Upload as your Google user: "
                "set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN "
                "(python -m app.drive_auth)."
            )
        if exc.resp.status != 404:
            return exc
        where = f" id={folder_id}" if folder_id else ""
        return DriveAccessError(
            f"Drive folder{where} not visible with drive.file scope. "
            f"That scope only sees folders this app created. "
            f"Run `python -m app.drive_auth` and paste the printed GDRIVE_* IDs. "
            f"You can then move those folders into Music in the Drive UI."
        )

    def assert_folders(self, folders: dict[str, str]) -> None:
        for label, folder_id in folders.items():
            if not folder_id:
                raise DriveAccessError(f"{label} is empty")
            try:
                meta = (
                    self._service.files()
                    .get(
                        fileId=folder_id,
                        fields="id,name,mimeType",
                        supportsAllDrives=True,
                    )
                    .execute()
                )
            except HttpError as exc:
                raise self._wrap_http_error(exc, folder_id) from exc
            log.info("drive %s ok name=%s", label, meta.get("name"))

    def create_folder(self, name: str, parent_id: str | None = None) -> tuple[str, str | None]:
        body: dict = {"name": name, "mimeType": FOLDER_MIME}
        if parent_id:
            body["parents"] = [parent_id]
        meta = (
            self._service.files()
            .create(body=body, fields="id,name,webViewLink", supportsAllDrives=True)
            .execute()
        )
        return meta["id"], meta.get("webViewLink")

    def ensure_named_folder(self, name: str) -> tuple[str, str | None]:
        query = (
            f"name = '{_escape_query(name)}' and mimeType = '{FOLDER_MIME}' "
            f"and trashed = false"
        )
        resp = (
            self._service.files()
            .list(
                q=query,
                spaces="drive",
                fields="files(id, name, webViewLink)",
                pageSize=5,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = resp.get("files") or []
        if files:
            return files[0]["id"], files[0].get("webViewLink")
        return self.create_folder(name)

    def file_exists(self, file_id: str) -> bool:
        try:
            meta = (
                self._service.files()
                .get(fileId=file_id, fields="id,trashed", supportsAllDrives=True)
                .execute()
            )
            return not meta.get("trashed")
        except HttpError as exc:
            if exc.resp.status == 404:
                return False
            raise

    def delete_file(self, file_id: str) -> None:
        try:
            self._service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
        except HttpError as exc:
            if exc.resp.status == 404:
                return
            raise

    def upload_tree(
        self,
        local_file: Path,
        root_folder_id: str,
        relative: Path,
        mime_type: str,
    ) -> tuple[str, str | None]:
        parts = list(relative.parts)
        if not parts:
            raise ValueError("empty relative path")
        filename = parts[-1]
        parent = root_folder_id
        try:
            for folder in parts[:-1]:
                parent = self._ensure_folder(parent, folder)
            return self._upload_or_replace(local_file, parent, filename, mime_type)
        except HttpError as exc:
            raise self._wrap_http_error(exc, root_folder_id) from exc

    def upload_with_retry(
        self,
        local_file: Path,
        root_folder_id: str,
        relative: Path,
        mime_type: str,
        attempts: int = 3,
    ) -> tuple[str, str | None]:
        last: Exception | None = None
        for i in range(attempts):
            try:
                return self.upload_tree(local_file, root_folder_id, relative, mime_type)
            except DriveAccessError:
                raise
            except HttpError as exc:
                last = self._wrap_http_error(exc, root_folder_id)
                if exc.resp.status in _NO_RETRY_STATUS or isinstance(last, DriveAccessError):
                    raise last from exc
                log.warning("drive upload attempt %s failed: %s", i + 1, exc)
            except Exception as exc:  # noqa: BLE001 — retry then surface
                last = exc
                log.warning("drive upload attempt %s failed: %s", i + 1, exc)
            time.sleep(2**i)
        assert last is not None
        raise last

    def _ensure_folder(self, parent_id: str, name: str) -> str:
        key = (parent_id, name)
        cached = self._folder_cache.get(key)
        if cached:
            return cached
        found = self._find_child(parent_id, name, folder=True)
        if found:
            self._folder_cache[key] = found
            return found
        meta = (
            self._service.files()
            .create(
                body={"name": name, "parents": [parent_id], "mimeType": FOLDER_MIME},
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        folder_id = meta["id"]
        self._folder_cache[key] = folder_id
        return folder_id

    def _find_child(self, parent_id: str, name: str, *, folder: bool) -> str | None:
        mime_clause = (
            f"and mimeType = '{FOLDER_MIME}'"
            if folder
            else f"and mimeType != '{FOLDER_MIME}'"
        )
        query = (
            f"name = '{_escape_query(name)}' and '{parent_id}' in parents "
            f"and trashed = false {mime_clause}"
        )
        resp = (
            self._service.files()
            .list(
                q=query,
                spaces="drive",
                fields="files(id, name)",
                pageSize=10,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = resp.get("files") or []
        return files[0]["id"] if files else None

    def _upload_or_replace(
        self,
        local_file: Path,
        parent_id: str,
        filename: str,
        mime_type: str,
    ) -> tuple[str, str | None]:
        media = MediaFileUpload(
            str(local_file),
            mimetype=mime_type,
            resumable=True,
            chunksize=8 * 1024 * 1024,
        )
        existing = self._find_child(parent_id, filename, folder=False)
        if existing:
            request = self._service.files().update(
                fileId=existing,
                media_body=media,
                fields="id,webViewLink",
                supportsAllDrives=True,
            )
        else:
            request = self._service.files().create(
                body={"name": filename, "parents": [parent_id]},
                media_body=media,
                fields="id,webViewLink",
                supportsAllDrives=True,
            )
        response = None
        while response is None:
            _status, response = request.next_chunk()
        return response["id"], response.get("webViewLink")
