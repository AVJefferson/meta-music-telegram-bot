from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

log = logging.getLogger(__name__)

DRIVE_SCOPE = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"


def _load_sa_info(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    path = Path(raw)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return json.loads(raw)


def _escape_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class DriveClient:
    def __init__(self, service_account_json: str) -> None:
        info = _load_sa_info(service_account_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=DRIVE_SCOPE)
        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        self._folder_cache: dict[tuple[str, str], str] = {}

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
        for folder in parts[:-1]:
            parent = self._ensure_folder(parent, folder)
        return self._upload_or_replace(local_file, parent, filename, mime_type)

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
