from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

from app.config import Settings
from app.drive_scopes import DRIVE_SCOPE

log = logging.getLogger(__name__)
FOLDER_MIME = "application/vnd.google-apps.folder"
_NO_RETRY_STATUS = {400, 401, 403, 404}


@dataclass
class DriveChild:
    id: str
    name: str
    mime_type: str
    size: int | None
    modified: str | None

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME


def _child_from_meta(meta: dict) -> DriveChild:
    size_raw = meta.get("size")
    size = int(size_raw) if size_raw is not None and str(size_raw).isdigit() else None
    return DriveChild(
        id=meta["id"],
        name=meta.get("name") or "",
        mime_type=meta.get("mimeType") or "",
        size=size,
        modified=meta.get("modifiedTime"),
    )


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

    def list_children(self, folder_id: str) -> list[DriveChild]:
        out: list[DriveChild] = []
        page_token: str | None = None
        while True:
            resp = (
                self._service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    spaces="drive",
                    fields="nextPageToken, files(id, name, mimeType, size, modifiedTime)",
                    pageSize=100,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            for meta in resp.get("files") or []:
                out.append(_child_from_meta(meta))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return out

    def find_name_conflicts(self, parent_id: str, filename: str) -> list[DriveChild]:
        hits = [c for c in self.list_children(parent_id) if c.name == filename and not c.is_folder]
        hits.sort(key=lambda c: c.modified or "", reverse=True)
        return hits

    def unused_name(self, parent_id: str, filename: str) -> str:
        names = {c.name for c in self.list_children(parent_id)}
        if filename not in names:
            return filename
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        n = 2
        while True:
            candidate = f"{stem} ({n}){suffix}"
            if candidate not in names:
                return candidate
            n += 1

    def get_child_meta(self, file_id: str) -> DriveChild | None:
        try:
            meta = (
                self._service.files()
                .get(
                    fileId=file_id,
                    fields="id,name,mimeType,size,modifiedTime,trashed",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            if exc.resp.status == 404:
                return None
            raise
        if meta.get("trashed"):
            return None
        return _child_from_meta(meta)

    def ensure_parent(self, root_folder_id: str, relative: Path) -> str:
        parts = list(relative.parts)
        if not parts:
            raise ValueError("empty relative path")
        parent = root_folder_id
        for folder in parts[:-1]:
            parent = self._ensure_folder(parent, folder)
        return parent

    def find_path(self, root_id: str, folder_parts: list[str]) -> str | None:
        parent = root_id
        for name in folder_parts:
            key = (parent, name)
            cached = self._folder_cache.get(key)
            if cached:
                parent = cached
                continue
            found = next(
                (c for c in self.list_children(parent) if c.name == name and c.is_folder),
                None,
            )
            if not found:
                return None
            self._folder_cache[key] = found.id
            parent = found.id
        return parent

    def download_bytes(self, file_id: str) -> bytes:
        request = self._service.files().get_media(fileId=file_id, supportsAllDrives=True)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk()
        return buf.getvalue()

    def create_file(
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
        return self._send_media(
            media,
            body={"name": filename, "parents": [parent_id]},
        )

    def replace_file(self, file_id: str, local_file: Path, mime_type: str) -> tuple[str, str | None]:
        media = MediaFileUpload(
            str(local_file),
            mimetype=mime_type,
            resumable=True,
            chunksize=8 * 1024 * 1024,
        )
        return self._send_media(media, file_id=file_id)

    def upload_bytes(
        self,
        data: bytes,
        parent_id: str,
        filename: str,
        mime_type: str,
        *,
        replace_id: str | None = None,
    ) -> tuple[str, str | None]:
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=False)
        body = {"name": filename, "parents": [parent_id]}
        return self._send_media(media, body=body, file_id=replace_id)

    def _send_media(
        self,
        media,
        *,
        body: dict | None = None,
        file_id: str | None = None,
    ) -> tuple[str, str | None]:
        if file_id:
            request = self._service.files().update(
                fileId=file_id,
                media_body=media,
                fields="id,webViewLink",
                supportsAllDrives=True,
            )
        else:
            request = self._service.files().create(
                body=body or {},
                media_body=media,
                fields="id,webViewLink",
                supportsAllDrives=True,
            )
        if media.resumable():
            response = None
            while response is None:
                _status, response = request.next_chunk()
            return response["id"], response.get("webViewLink")
        meta = request.execute()
        return meta["id"], meta.get("webViewLink")

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
        found = next((c for c in self.list_children(parent_id) if c.name == name and c.is_folder), None)
        if found:
            self._folder_cache[key] = found.id
            return found.id
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

    def _upload_or_replace(
        self,
        local_file: Path,
        parent_id: str,
        filename: str,
        mime_type: str,
    ) -> tuple[str, str | None]:
        conflicts = self.find_name_conflicts(parent_id, filename)
        if conflicts:
            return self.replace_file(conflicts[0].id, local_file, mime_type)
        return self.create_file(local_file, parent_id, filename, mime_type)
