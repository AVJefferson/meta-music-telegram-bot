from __future__ import annotations

import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from app.catalog import Catalog
from app.covers import add_cover_option, cover_wait_role, finalize_cover_labels
from app.queue import _delete_cover_choice_gallery, cover_park_role
from app.review_ui import cover_option_text, format_cover_prompt


def _jpeg(size: tuple[int, int] = (1200, 1200), color: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _pending_kwargs(**overrides) -> dict:
    base = dict(
        phase="cover",
        local_path="/tmp/source.flac",
        sidecar_path=None,
        relative_path=None,
        kind="library",
        original_json="{}",
        recommended_json="{}",
        working_json="{}",
        candidates_json="[]",
        identity_json="{}",
        source_report_json="{}",
        chat_id=-100123,
        thread_id=1,
        status_message_id=4,
        topic_name="General",
        file_name="source.flac",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    base.update(overrides)
    return base


class CoverWaitRoleTests(unittest.TestCase):
    def test_roles(self) -> None:
        self.assertEqual(cover_wait_role("a", None), "leader")
        self.assertEqual(cover_wait_role("a", "a"), "follower")
        self.assertEqual(cover_wait_role("b", "a"), "queued")


class CoverParkRoleTests(unittest.TestCase):
    def test_different_album_queues_when_leader_busy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            catalog.insert_pending_review(
                **_pending_kwargs(
                    source_report_json=json.dumps(
                        {"cover_picker": {"album_key": "aaa", "role": "leader"}}
                    )
                )
            )
            ctx = SimpleNamespace(catalog=catalog)
            self.assertEqual(cover_park_role(ctx, "aaa"), "follower")
            self.assertEqual(cover_park_role(ctx, "bbb"), "queued")

    def test_processing_leader_still_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            pending_id = catalog.insert_pending_review(
                **_pending_kwargs(
                    source_report_json=json.dumps(
                        {"cover_picker": {"album_key": "aaa", "role": "leader"}}
                    )
                )
            )
            catalog.update_pending_review(pending_id, status="processing")
            ctx = SimpleNamespace(catalog=catalog)
            self.assertEqual(cover_park_role(ctx, "bbb"), "queued")
            self.assertEqual(cover_park_role(ctx, "bbb", excluding_id=pending_id), "leader")


class CoverLabelTests(unittest.TestCase):
    def test_equal_images_join_source_labels(self) -> None:
        payload = _jpeg()
        options = []
        add_cover_option(options, payload, "file")
        add_cover_option(options, payload, "itunes")
        self.assertEqual(len(options), 1)
        self.assertEqual(options[0].label, "file == iTunes")
        self.assertEqual(options[0].width, 1200)
        self.assertEqual(options[0].height, 1200)

    def test_caa_itunes_and_file_triple(self) -> None:
        payload = _jpeg((800, 800), (1, 2, 3))
        options = []
        add_cover_option(options, payload, "file")
        add_cover_option(options, payload, "caa")
        add_cover_option(options, payload, "itunes")
        self.assertEqual(options[0].label, "file == CAA == iTunes")

    def test_identical_caa_stays_caa(self) -> None:
        payload = _jpeg((640, 640), (4, 5, 6))
        options = []
        add_cover_option(options, payload, "caa")
        add_cover_option(options, payload, "caa")
        finalize_cover_labels(options)
        self.assertEqual(len(options), 1)
        self.assertEqual(options[0].label, "CAA")

    def test_distinct_caa_are_numbered(self) -> None:
        options = []
        add_cover_option(options, _jpeg((100, 100), (1, 0, 0)), "caa")
        add_cover_option(options, _jpeg((100, 100), (0, 1, 0)), "caa")
        finalize_cover_labels(options)
        self.assertEqual([opt.label for opt in options], ["CAA 1", "CAA 2"])

    def test_caption_includes_size(self) -> None:
        option = {"label": "file == iTunes", "width": 1200, "height": 1200}
        self.assertEqual(cover_option_text(0, option), "1. file == iTunes (1200x1200)")
        prompt = format_cover_prompt("Album", "Artist", [option], "song.flac")
        self.assertIn("1. file == iTunes (1200x1200)", prompt)


class CoverChoiceHoldTests(unittest.IsolatedAsyncioTestCase):
    async def test_holds_chosen_cover_for_settings_seconds(self) -> None:
        deleted: list[int] = []
        slept: list[float] = []

        class Bot:
            async def delete_message(self, *, chat_id, message_id):
                del chat_id
                deleted.append(message_id)

        ctx = SimpleNamespace(
            settings=SimpleNamespace(cover_choice_hold_seconds=2.0),
            bot=Bot(),
        )
        picker = {
            "options": [{"message_id": 10}, {"message_id": 11}],
            "media_message_ids": [10, 11],
        }

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        with patch("app.queue.asyncio.sleep", fake_sleep):
            await _delete_cover_choice_gallery(ctx, -100, picker, 1, delay_chosen=True)
        self.assertEqual(deleted, [10, 11])
        self.assertEqual(slept, [2.0])


if __name__ == "__main__":
    unittest.main()
