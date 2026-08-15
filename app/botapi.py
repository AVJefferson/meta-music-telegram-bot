from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

# In local mode the Bot API server keeps every downloaded file forever and offers
# no API to remove them, so the bot is responsible for cleaning up. Layout is
# <root>/<token>/<kind>/file_N.ext, with the server's own *.binlog files sitting
# directly in the token directory.
BOT_API_ROOT = Path("/var/lib/telegram-bot-api")


def _inside_root(path: Path) -> Path | None:
    try:
        resolved = path.resolve()
        resolved.relative_to(BOT_API_ROOT)
    except (ValueError, OSError):
        return None
    return resolved


def discard_download(file_path: str | None) -> None:
    """Remove a file the local Bot API server downloaded for us."""
    if not file_path:
        return
    candidate = Path(file_path)
    if not candidate.is_absolute():
        return
    resolved = _inside_root(candidate)
    if resolved is None or resolved.suffix == ".binlog":
        return
    try:
        resolved.unlink(missing_ok=True)
    except OSError:
        log.debug("bot-api file unlink failed %s", resolved, exc_info=True)


def sweep_downloads(older_than_hours: float = 24.0) -> int:
    """Drop leftovers from crashed jobs and from before deletion was wired up."""
    if not BOT_API_ROOT.is_dir():
        return 0
    cutoff = time.time() - older_than_hours * 3600.0
    removed = 0
    for token_dir in BOT_API_ROOT.iterdir():
        if not token_dir.is_dir():
            continue
        # Only inside the per-kind subdirectories: the token directory itself
        # holds the server's binlogs.
        for kind_dir in token_dir.iterdir():
            if not kind_dir.is_dir():
                continue
            for path in kind_dir.rglob("*"):
                if not path.is_file() or path.suffix == ".binlog":
                    continue
                try:
                    if path.stat().st_mtime <= cutoff:
                        path.unlink()
                        removed += 1
                except OSError:
                    log.debug("bot-api sweep failed %s", path, exc_info=True)
    return removed
