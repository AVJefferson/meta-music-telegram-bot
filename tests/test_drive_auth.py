from __future__ import annotations

import errno
import socket
import unittest
from unittest.mock import patch

from app import drive_auth


class PortBusyTests(unittest.TestCase):
    def test_can_bind_false_when_held(self) -> None:
        held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        try:
            self.assertFalse(drive_auth._can_bind("127.0.0.1", port))
        finally:
            held.close()
        self.assertTrue(drive_auth._can_bind("127.0.0.1", port))

    def test_next_free_port_skips_occupied(self) -> None:
        held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        try:
            nxt = drive_auth._next_free_port("127.0.0.1", port)
            self.assertNotEqual(nxt, port)
            self.assertTrue(drive_auth._can_bind("127.0.0.1", nxt))
        finally:
            held.close()

    def test_is_addr_in_use(self) -> None:
        self.assertTrue(drive_auth._is_addr_in_use(OSError(errno.EADDRINUSE, "busy")))
        self.assertFalse(drive_auth._is_addr_in_use(OSError(errno.ECONNREFUSED, "no")))
        self.assertFalse(drive_auth._is_addr_in_use(ValueError("no")))

    def test_format_listeners(self) -> None:
        self.assertIn("TIME_WAIT", drive_auth._format_listeners([]))
        self.assertEqual(drive_auth._format_listeners([(12, "python")]), "pid 12 (python)")

    def test_resolve_bind_port_free(self) -> None:
        port = drive_auth._next_free_port("127.0.0.1", 18090)
        self.assertEqual(drive_auth._resolve_bind_port("127.0.0.1", port), port)

    def test_resolve_picks_next_on_n(self) -> None:
        held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        try:
            with (
                patch("app.drive_auth.sys.stdin.isatty", return_value=True),
                patch("app.drive_auth._prompt_busy_choice", return_value="n"),
                patch("app.drive_auth._listeners_on_port", return_value=[(1, "python")]),
            ):
                nxt = drive_auth._resolve_bind_port("127.0.0.1", port)
            self.assertNotEqual(nxt, port)
            self.assertTrue(drive_auth._can_bind("127.0.0.1", nxt))
        finally:
            held.close()

    def test_resolve_kills_and_reuses(self) -> None:
        with (
            patch("app.drive_auth._can_bind", side_effect=[False, True]),
            patch("app.drive_auth.sys.stdin.isatty", return_value=True),
            patch("app.drive_auth._prompt_busy_choice", return_value="k"),
            patch("app.drive_auth._listeners_on_port", return_value=[(4242, "python")]),
            patch("app.drive_auth._kill_pids") as kill,
            patch("app.drive_auth._wait_can_bind", return_value=True),
        ):
            self.assertEqual(drive_auth._resolve_bind_port("localhost", 8090), 8090)
        kill.assert_called_once_with([4242])

    def test_resolve_quit(self) -> None:
        with (
            patch("app.drive_auth._can_bind", return_value=False),
            patch("app.drive_auth.sys.stdin.isatty", return_value=True),
            patch("app.drive_auth._prompt_busy_choice", return_value="q"),
            patch("app.drive_auth._listeners_on_port", return_value=[]),
            self.assertRaises(SystemExit) as ctx,
        ):
            drive_auth._resolve_bind_port("localhost", 8090)
        self.assertEqual(ctx.exception.code, 1)

    def test_resolve_non_tty_auto_picks(self) -> None:
        held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        try:
            with (
                patch("app.drive_auth.sys.stdin.isatty", return_value=False),
                patch("app.drive_auth._listeners_on_port", return_value=[]),
            ):
                nxt = drive_auth._resolve_bind_port("127.0.0.1", port)
            self.assertNotEqual(nxt, port)
        finally:
            held.close()

    def test_kill_refuses_empty_then_new(self) -> None:
        held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        try:
            with (
                patch("app.drive_auth.sys.stdin.isatty", return_value=True),
                patch("app.drive_auth._prompt_busy_choice", side_effect=["k", "n"]),
                patch("app.drive_auth._listeners_on_port", return_value=[]),
                patch("app.drive_auth._kill_pids") as kill,
            ):
                nxt = drive_auth._resolve_bind_port("127.0.0.1", port)
            kill.assert_not_called()
            self.assertNotEqual(nxt, port)
        finally:
            held.close()


if __name__ == "__main__":
    unittest.main()
