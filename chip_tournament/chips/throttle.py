"""Locks out anyone who keeps guessing team PINs or the director password."""

from __future__ import annotations

import threading
import time


class Throttle:
    """Allows `limit` wrong attempts per key within `window` seconds, then blocks the key."""

    def __init__(self, limit: int, window: int):
        self.limit, self.window = limit, window
        self.fails: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [at for at in self.fails.get(key, []) if now - at < self.window]
        if recent:
            self.fails[key] = recent
        else:
            self.fails.pop(key, None)
        return recent

    def wait(self, key: str) -> int:
        """Seconds until `key` may try again (0 if it isn't blocked)."""
        with self.lock:
            now = time.monotonic()
            recent = self._recent(key, now)
            if len(recent) < self.limit:
                return 0
            return max(1, int(recent[-self.limit] + self.window - now + 0.999))

    def failures(self, key: str) -> int:
        with self.lock:
            return len(self._recent(key, time.monotonic()))

    def fail(self, key: str) -> None:
        with self.lock:
            self._recent(key, time.monotonic())
            self.fails.setdefault(key, []).append(time.monotonic())

    def clear(self, key: str) -> None:
        with self.lock:
            self.fails.pop(key, None)
