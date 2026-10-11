from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock

from app.errors import AppError

_lock = Lock()
_hits: dict[tuple[str, int], list[float]] = defaultdict(list)


def _window(key: str, subject_id: int, now: float) -> list[float]:
    return [stamp for stamp in _hits[(key, subject_id)] if now - stamp < 60.0]


def retry_after(key: str, subject_id: int, limit_per_minute: int) -> float:
    """Reserve one call. Return seconds to wait when the window is full."""
    if limit_per_minute <= 0:
        return 0.0
    now = time.monotonic()
    with _lock:
        stamps = _window(key, subject_id, now)
        if len(stamps) >= limit_per_minute:
            _hits[(key, subject_id)] = stamps
            return max(0.1, 60.0 - (now - stamps[0]))
        stamps.append(now)
        _hits[(key, subject_id)] = stamps
        return 0.0


def check_rate(key: str, subject_id: int, limit_per_minute: int) -> None:
    if limit_per_minute <= 0:
        return
    delay = retry_after(key, subject_id, limit_per_minute)
    if delay > 0:
        raise AppError("rate_limited")
    if len(_hits) > 4096:
        now = time.monotonic()
        with _lock:
            for stale in [k for k, times in _hits.items() if not times or now - times[-1] > 60.0]:
                _hits.pop(stale, None)


def reset() -> None:
    with _lock:
        _hits.clear()
