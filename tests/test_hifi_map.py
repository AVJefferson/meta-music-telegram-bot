from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.errors import AppError
from app.hifi import _lock, _sessions, pick_song, strip_hifi_keyboard, user_keyboard
from tests.support import make_ctx, temp_catalog


class HifiMapTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        _sessions.clear()

    def test_user_keyboard_never_exposes_raw_callback(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            ctx = make_ctx(catalog)
            markup = user_keyboard(ctx, 7, "sess", [("Title", "RAW-FROM-HIFI")])
            data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
            self.assertTrue(all(not (item and "RAW-FROM-HIFI" in item) for item in data))
            self.assertTrue(any(item.startswith("hf:") for item in data if item))

    async def test_other_user_pick_ignored(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            ctx = make_ctx(catalog)
            markup = user_keyboard(ctx, 1, "sess", [("Title", "cb")])
            pick_id = markup.inline_keyboard[0][0].callback_data[3:]
            _sessions["sess"] = {"user_id": 1, "client": SimpleNamespace(), "nav": []}
            with self.assertRaises(AppError) as err:
                await pick_song(ctx, 2, pick_id)
            self.assertEqual(err.exception.code, "not_found")

    async def test_busy_when_lock_held(self) -> None:
        from app.hifi import search_songs

        directory, catalog = temp_catalog()
        with directory:
            ctx = make_ctx(catalog, hifi_queue_max=1)
            _sessions["busy"] = {"user_id": 1}
            await _lock.acquire()
            try:
                with self.assertRaises(AppError) as err:
                    await search_songs(ctx, 9, "q")
                self.assertEqual(err.exception.code, "busy")
            finally:
                if _lock.locked():
                    _lock.release()
            _sessions.clear()

    def test_strip(self) -> None:
        rows = [[SimpleNamespace(text="Next »", data="n"), SimpleNamespace(text="Real", data="r")]]
        self.assertEqual(strip_hifi_keyboard(rows), [("Real", "r")])
