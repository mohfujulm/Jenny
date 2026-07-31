from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable


_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class BrowserSessionRegistry:
    """Tracks browser tabs that currently have the Ask Jenny UI loaded."""

    def __init__(
        self,
        ttl_seconds: float = 90,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._last_seen: dict[str, float] = {}

    def heartbeat(self, session_id: str) -> int:
        normalized_id = self._normalize_session_id(session_id)
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            self._last_seen[normalized_id] = now
            return len(self._last_seen)

    def remove(self, session_id: str) -> int:
        normalized_id = self._normalize_session_id(session_id)
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            self._last_seen.pop(normalized_id, None)
            return len(self._last_seen)

    def active_count(self) -> int:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            return len(self._last_seen)

    @staticmethod
    def _normalize_session_id(session_id: str) -> str:
        normalized_id = str(session_id or "").strip()
        if not _SESSION_ID_PATTERN.fullmatch(normalized_id):
            raise ValueError("Invalid browser session ID.")
        return normalized_id

    def _prune_locked(self, now: float) -> None:
        expired_ids = [
            session_id
            for session_id, last_seen in self._last_seen.items()
            if now - last_seen > self._ttl_seconds
        ]
        for session_id in expired_ids:
            self._last_seen.pop(session_id, None)
