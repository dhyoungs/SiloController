"""
Persistent log of all inbound API messages (HTTP, MQTT, GUI).

Stores entries as one JSON object per line in `logs/api_messages.jsonl`.
Rotates when the file exceeds ~2 MB — renames to `.1` and starts fresh.
Thread-safe; designed to be imported and called from any subsystem.
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_FILE = LOG_DIR / "api_messages.jsonl"
MAX_BYTES = 2 * 1024 * 1024  # 2 MB before rotation

_lock = threading.Lock()


def _ensure_dir() -> None:
    LOG_DIR.mkdir(exist_ok=True)


def log_api_message(
    *,
    source: str,        # "web", "mqtt", "gui"
    action: str,        # "open", "close", "message", "record/start", etc.
    payload: str = "",  # the raw payload or message text
    result: str = "",   # brief outcome, e.g. "ok", "ignored: already open"
    remote_addr: str = "",  # client IP when available
) -> None:
    """Append one entry to the persistent API log."""
    entry = {
        "ts": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z",
        "source": source,
        "action": action,
        "payload": payload,
        "result": result,
        "remote_addr": remote_addr,
    }
    line = json.dumps(entry, separators=(",", ":")) + "\n"

    with _lock:
        try:
            _ensure_dir()
            # Rotate if needed
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_BYTES:
                rotated = LOG_FILE.with_suffix(".1")
                try:
                    rotated.unlink(missing_ok=True)
                except TypeError:
                    # Python 3.7 compat
                    if rotated.exists():
                        rotated.unlink()
                LOG_FILE.rename(rotated)
                logger.info("API log rotated to %s", rotated.name)
            with open(LOG_FILE, "a") as fh:
                fh.write(line)
        except Exception as exc:
            logger.warning("Failed to write API log: %s", exc)


def read_log(limit: int = 200, offset: int = 0) -> list[dict]:
    """
    Read the most recent `limit` entries (newest first).
    Returns list of dicts.
    """
    with _lock:
        if not LOG_FILE.exists():
            return []
        try:
            with open(LOG_FILE, "r") as fh:
                lines = fh.readlines()
        except Exception:
            return []

    entries = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(entries) >= offset + limit:
            break

    return entries[offset:offset + limit]


def log_count() -> int:
    """Total lines in the current log file."""
    with _lock:
        if not LOG_FILE.exists():
            return 0
        try:
            with open(LOG_FILE, "rb") as fh:
                return sum(1 for _ in fh)
        except Exception:
            return 0
