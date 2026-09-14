from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.catalog import Catalog
from app.drive import DriveHub, require_drive, resolve_drive
from app.relocate import ensure_local_flac
from tests.support import settings


class ResolveDriveTests(unittest.TestCase):
    def test_existing_client_is_not_treated_as_hub(self) -> None:
        class Client:
            @classmethod
            def for_user(cls, *args, **kwargs):
                raise AssertionError("DriveClient.for_user must not run")

            def find_path(self, *_a, **_k):
                return "ok"

            def download_to(self, *_a, **_k):
                return None

        client = Client()
        ctx = SimpleNamespace(drive=client, index_user_id=7)
        self.assertIs(resolve_drive(ctx, 7), client)
        self.assertIs(require_drive(ctx, 7, method="download_to"), client)

    def test_hub_resolves_per_user_and_skips_without_uid(self) -> None:
        directory = tempfile.TemporaryDirectory()
        with directory:
            catalog = Catalog(Path(directory.name) / "state.sqlite")
            hub = DriveHub(settings(), catalog)
            client = SimpleNamespace(
                download_to=lambda *_a, **_k: None,
                find_path=lambda *_a, **_k: "p",
            )
            ctx = SimpleNamespace(drive=hub)
            with patch.object(hub, "for_user", return_value=client) as getter:
                self.assertIs(resolve_drive(ctx, 42), client)
                getter.assert_called_once_with(42)
                self.assertIsNone(resolve_drive(ctx, 0))
                self.assertIs(require_drive(ctx, 42), client)
                self.assertIsNone(require_drive(ctx, 0))
            catalog.close()

    def test_require_drive_never_returns_hub(self) -> None:
        directory = tempfile.TemporaryDirectory()
        with directory:
            catalog = Catalog(Path(directory.name) / "state.sqlite")
            hub = DriveHub(settings(), catalog)
            ctx = SimpleNamespace(drive=hub, index_user_id=0)
            self.assertIsNone(require_drive(ctx, 0, method="download_to"))
            self.assertIsNone(require_drive(ctx, 99, method="download_to"))
            catalog.close()


class EnsureLocalFlacHubTests(unittest.IsolatedAsyncioTestCase):
    async def test_hub_without_client_raises_instead_of_attribute_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            catalog = Catalog(root / "state.sqlite")
            track_id = catalog.insert_pending(
                kind="library",
                mb_recording_id=None,
                acoustid=None,
                local_path=str(root / "gone.flac"),
                sidecar_path=None,
                relative_path="English/A/LP/a.flac",
                bit_depth=16,
                sample_rate=44100,
                title="Go",
                artist="A",
                album="LP",
                status="uploaded",
                drive_file_id="drive-1",
                user_id=0,
            )
            track = catalog.get_track(track_id)
            assert track is not None
            hub = DriveHub(settings(), catalog)
            ctx = SimpleNamespace(
                settings=SimpleNamespace(
                    library_root=root / "library",
                    review_root=root / "review",
                    pending_root=root / "pending",
                    gdrive_folder_id="lib",
                    gdrive_review_folder_id="rev",
                ),
                catalog=catalog,
                drive=hub,
                index_user_id=0,
            )
            with self.assertRaises(FileNotFoundError) as raised:
                await ensure_local_flac(ctx, track)
            self.assertNotIn("download_to", str(raised.exception))
            catalog.close()

    async def test_hub_for_user_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            catalog = Catalog(root / "state.sqlite")
            track_id = catalog.insert_pending(
                kind="library",
                mb_recording_id=None,
                acoustid=None,
                local_path=str(root / "gone.flac"),
                sidecar_path=None,
                relative_path="English/A/LP/a.flac",
                bit_depth=16,
                sample_rate=44100,
                title="Go",
                artist="A",
                album="LP",
                status="uploaded",
                drive_file_id="drive-1",
                user_id=9,
            )
            track = catalog.get_track(track_id)
            assert track is not None

            class Client:
                def download_to(self, file_id: str, dest: Path) -> Path:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(b"flac")
                    return dest

            hub = DriveHub(settings(), catalog)
            ctx = SimpleNamespace(
                settings=SimpleNamespace(
                    library_root=root / "library",
                    review_root=root / "review",
                    pending_root=root / "pending",
                    gdrive_folder_id="lib",
                    gdrive_review_folder_id="rev",
                ),
                catalog=catalog,
                drive=hub,
            )
            with patch.object(hub, "for_user", return_value=Client()):
                path = await ensure_local_flac(ctx, track)
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_bytes(), b"flac")
            catalog.close()
