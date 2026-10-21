"""Saves the tournament as JSON on disk, with an undo history."""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .engine import Tournament

HISTORY_LIMIT = 100


class Store:
    def __init__(self, data_dir: str | os.PathLike):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "tournament.json"
        self.history_path = self.dir / "history.json"
        self.lock = threading.RLock()

    def load(self) -> Tournament:
        if not self.path.exists():
            return Tournament()
        return Tournament.from_dict(json.loads(self.path.read_text()))

    def _write(self, path: Path, data) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(path)

    def _history(self) -> list[dict]:
        if not self.history_path.exists():
            return []
        return json.loads(self.history_path.read_text())

    @contextmanager
    def change(self, label: str):
        """Load, let the caller change the tournament, then save.

        Nothing is saved if the caller raises, so a rule error never leaves a
        half-applied change behind. The state before the change is kept for undo.
        """
        with self.lock:
            before = self.path.read_text() if self.path.exists() else None
            t = self.load()
            yield t
            if before is not None:
                history = self._history()
                history.append({"label": label, "at": datetime.now().isoformat(timespec="seconds"), "state": before})
                self._write(self.history_path, history[-HISTORY_LIMIT:])
            self._write(self.path, t.to_dict())

    def last_undo_label(self) -> str | None:
        history = self._history()
        return history[-1]["label"] if history else None

    def undo(self) -> str | None:
        with self.lock:
            history = self._history()
            if not history:
                return None
            entry = history.pop()
            self.path.write_text(entry["state"])
            self._write(self.history_path, history)
            return entry["label"]

    def archive_and_reset(self) -> None:
        with self.lock:
            if self.path.exists():
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                self.path.rename(self.dir / f"tournament-{stamp}.json")
            if self.history_path.exists():
                self.history_path.unlink()
