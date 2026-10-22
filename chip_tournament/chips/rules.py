"""League rule sets (8-Ball, 9-Ball, ...) that players can read and search.

Rules are kept in their own file, separate from the tournament, so starting a
new tournament doesn't erase them. The director pastes plain text; a few
simple marks give it structure:

    # Heading            -> section heading
    ## Smaller heading   -> sub-heading
    - item / * item      -> bullet list
    1. item / 1) item    -> numbered list
    **words**            -> bold
    any other line       -> its own paragraph
"""

from __future__ import annotations

import json
import re
import threading
from html import escape
from pathlib import Path

DEFAULT_RULE_SETS = ["8-Ball Rules", "9-Ball Rules", "Fast 8 Rules", "Fast Saratoga Rules"]


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "rules"


class RuleBook:
    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "rules.json"
        self.lock = threading.RLock()

    def load(self) -> list[dict]:
        if not self.path.exists():
            return [{"id": slugify(t), "title": t, "body": ""} for t in DEFAULT_RULE_SETS]
        return json.loads(self.path.read_text())

    def _save(self, sets: list[dict]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sets, indent=1))
        tmp.replace(self.path)

    def get(self, rule_id: str) -> dict | None:
        return next((r for r in self.load() if r["id"] == rule_id), None)

    def save(self, rule_id: str, title: str, body: str) -> dict:
        title = title.strip()
        if not title:
            raise ValueError("Give the rule set a title.")
        with self.lock:
            sets = self.load()
            rule = next((r for r in sets if r["id"] == rule_id), None)
            if rule is None:
                raise ValueError("That rule set no longer exists.")
            rule["title"], rule["body"] = title, body.replace("\r\n", "\n").strip()
            self._save(sets)
            return rule

    def add(self, title: str) -> dict:
        title = title.strip() or "New rules"
        with self.lock:
            sets = self.load()
            base, n, used = slugify(title), 2, {r["id"] for r in sets}
            rule_id = base
            while rule_id in used:
                rule_id, n = f"{base}-{n}", n + 1
            rule = {"id": rule_id, "title": title, "body": ""}
            sets.append(rule)
            self._save(sets)
            return rule

    def delete(self, rule_id: str) -> None:
        with self.lock:
            self._save([r for r in self.load() if r["id"] != rule_id])

    def move(self, rule_id: str, step: int) -> None:
        with self.lock:
            sets = self.load()
            i = next((i for i, r in enumerate(sets) if r["id"] == rule_id), None)
            j = None if i is None else i + step
            if i is not None and 0 <= j < len(sets):
                sets[i], sets[j] = sets[j], sets[i]
                self._save(sets)


def _inline(text: str) -> str:
    """Escape text, then turn **words** into bold."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escape(text))


def render(body: str) -> str:
    """Turn the director's plain text into safe HTML blocks (each one searchable)."""
    out: list[str] = []
    list_tag = None

    def close_list():
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line:
            close_list()
            continue
        if line.startswith("## "):
            close_list()
            out.append(f'<h4 class="rule-block">{_inline(line[3:])}</h4>')
        elif line.startswith("# "):
            close_list()
            out.append(f'<h3 class="rule-block">{_inline(line[2:])}</h3>')
        elif re.match(r"^[-*•]\s+", line):
            if list_tag != "ul":
                close_list()
                out.append("<ul>")
                list_tag = "ul"
            out.append(f'<li class="rule-block">{_inline(re.sub(r"^[-*•]\s+", "", line))}</li>')
        elif re.match(r"^\d+[.)]\s+", line):
            if list_tag != "ol":
                close_list()
                out.append("<ol>")
                list_tag = "ol"
            out.append(f'<li class="rule-block">{_inline(re.sub(r"^\d+[.)]\s+", "", line))}</li>')
        else:
            close_list()
            out.append(f'<p class="rule-block">{_inline(line)}</p>')
    close_list()
    return "\n".join(out)
