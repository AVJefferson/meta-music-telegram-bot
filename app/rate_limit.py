from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock

from app.errors import AppError

_lock = Lock()
_hits: dict[tuple[str, int], list[float]] = defaultdict(list)


def check_rate(key: str, subject_id: int, limit_per_minute: int) -> None:
    if limit_per_minute <= 0:
        return
    now = time.monotonic()
    window = 60.0
    with _lock:
        stamps = [t for t in _hits[(key, subject_id)] if now - t < window]
        if len(stamps) >= limit_per_minute:
            _hits[(key, subject_id)] = stamps
            raise AppError("rate_limited")
        stamps.append(now)
        _hits[(key, subject_id)] = stamps
        if len(_hits) > 4096:
            for stale in [k for k, times in _hits.items() if not times or now - times[-1] > window]:
                _hits.pop(stale, None)


def reset() -> None:
    with _lock:
        _hits.clear()
