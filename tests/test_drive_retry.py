from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from googleapiclient.errors import HttpError

from app.drive import DriveAccessError, retry_drive_io


def _http_error(status: int) -> HttpError:
    return HttpError(SimpleNamespace(status=status, reason="err"), b"err")


class RetryDriveIoTests(unittest.TestCase):
    def test_retries_dropped_connection_then_succeeds(self) -> None:
        calls = {"n": 0}

        def op() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise AttributeError("'NoneType' object has no attribute 'read'")
            return "ok"

        with patch("app.drive.time.sleep"):
            self.assertEqual(retry_drive_io(op, label="download"), "ok")
        self.assertEqual(calls["n"], 3)

    def test_does_not_retry_404(self) -> None:
        calls = {"n": 0}

        def op() -> str:
            calls["n"] += 1
            raise _http_error(404)

        with self.assertRaises(HttpError):
            retry_drive_io(op, label="download")
        self.assertEqual(calls["n"], 1)

    def test_does_not_retry_drive_access_error(self) -> None:
        calls = {"n": 0}

        def op() -> str:
            calls["n"] += 1
            raise DriveAccessError("quota")

        with self.assertRaises(DriveAccessError):
            retry_drive_io(op, label="download")
        self.assertEqual(calls["n"], 1)

    def test_unlinks_partial_file_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "song.flac"
            calls = {"n": 0}

            def op() -> Path:
                calls["n"] += 1
                dest.write_bytes(b"partial")
                if calls["n"] < 2:
                    raise AttributeError("'NoneType' object has no attribute 'read'")
                dest.write_bytes(b"full")
                return dest

            def on_fail() -> None:
                dest.unlink(missing_ok=True)

            with patch("app.drive.time.sleep"):
                result = retry_drive_io(op, label="download", on_fail=on_fail)
            self.assertEqual(result.read_bytes(), b"full")
            self.assertEqual(calls["n"], 2)
