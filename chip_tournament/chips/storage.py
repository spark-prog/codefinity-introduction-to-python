"""Saves the tournament as JSON on disk, with an undo history."""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .engine import Tournament

HISTORY_LIMIT = 40  # each step is a full copy of the tournament, rewritten on every action


class Store:
    def __init__(self, data_dir: str | os.PathLike, kind=Tournament):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "tournament.json"
        self.history_path = self.dir / "history.json"
        self.lock = threading.RLock()
        self.kind = kind  # the tournament class: chip Tournament or BracketTournament

    def load(self):
        if not self.path.exists():
            return self.kind()
        return self.kind.from_dict(json.loads(self.path.read_text()))

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
                # Every step gets a number that only goes up (even when old steps are dropped), so a
                # stale Undo button can always be told apart from the latest action.
                seq = max([h.get("seq", 0) for h in history] + [self._last_seq()]) + 1
                self._write(self.dir / "undo_seq.json", seq)
                history.append({"label": label, "at": datetime.now().isoformat(timespec="seconds"), "state": before,
                                "seq": seq})
                self._write(self.history_path, history[-HISTORY_LIMIT:])
            self._write(self.path, t.to_dict())

    def save(self, t: Tournament) -> None:
        """Save without adding an undo step."""
        with self.lock:
            self._write(self.path, t.to_dict())

    def _last_seq(self) -> int:
        path = self.dir / "undo_seq.json"
        try:
            return int(json.loads(path.read_text())) if path.exists() else 0
        except (ValueError, TypeError):
            return 0

    def undo_depth(self) -> int:
        """The number of the latest undoable step; changes with every action, so a stale Undo button can be spotted."""
        history = self._history()
        if not history:
            return 0
        return history[-1].get("seq") or len(history)

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
        """Start over, keeping the old tournament (and its texts) as an archive."""
        with self.lock:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            if self.path.exists():
                self.path.rename(self.dir / f"tournament-{stamp}.json")
            texts = self.dir / "texts.log"
            if texts.exists():
                texts.rename(self.dir / f"texts-{stamp}.log")
            self._clear_working_files()

    def delete_and_reset(self) -> None:
        """Start over, permanently deleting the current tournament and its texts."""
        with self.lock:
            self.path.unlink(missing_ok=True)
            (self.dir / "texts.log").unlink(missing_ok=True)
            self._clear_working_files()

    def _clear_working_files(self) -> None:
        self.history_path.unlink(missing_ok=True)  # undo steps hold full copies of the roster
        (self.dir / "import.csv").unlink(missing_ok=True)

    def archives(self) -> list[dict]:
        """Past tournaments kept on disk, newest first."""
        out = []
        for path in sorted(self.dir.glob("tournament-*.json"), reverse=True):
            stamp = path.stem[len("tournament-"):]
            try:
                data = json.loads(path.read_text())
                name = data.get("settings", {}).get("name", "")
                teams = len(data.get("teams") or data.get("entries") or {})
            except (OSError, ValueError):
                name, teams = "", 0
            try:
                archived = datetime.strptime(stamp, "%Y%m%d-%H%M%S")
            except ValueError:
                archived = None
            out.append({"stamp": stamp, "name": name or "Tournament", "teams": teams, "archived": archived})
        return out

    def delete_archive(self, stamp: str) -> bool:
        if not stamp or not all(c.isdigit() or c == "-" for c in stamp):
            return False
        with self.lock:
            path = self.dir / f"tournament-{stamp}.json"
            found = path.exists()
            path.unlink(missing_ok=True)
            (self.dir / f"texts-{stamp}.log").unlink(missing_ok=True)
            return found
