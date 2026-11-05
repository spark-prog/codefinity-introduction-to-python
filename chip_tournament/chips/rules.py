"""League rule sets (8-Ball, 9-Ball, ...) that players can read and search.

Rules are kept in their own file, separate from the tournament, so starting a
new tournament doesn't erase them. The director pastes plain text; a few
simple marks give it structure:

    # Heading            -> section heading
    ## Smaller heading   -> sub-heading
    - item / * item      -> bullet list
    1. item / 1) item    -> numbered list
    **words**            -> bold
    [apa: Fouls]         -> a link to that page of the official APA rules
    any other line       -> its own paragraph
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from html import escape, unescape
from pathlib import Path

from . import apa_rules

DEFAULT_RULE_SETS = ["8-Ball Rules", "9-Ball Rules", "Fast 8 Rules", "Fast Saratoga Rules"]

APA_LINK = re.compile(r"\[apa:\s*([^\]]*?)\s*\]", re.IGNORECASE)

# Pool shorthand: a search for any word on a line also finds the others on that line.
DEFAULT_SHORTHAND = """BIH = ball in hand
SL = skill level
DS = defensive shot, safety
TO = timeout, time out
combo = combination
lag = lagging
8 = 8-ball, eight ball
9 = 9-ball, nine ball"""

DEFAULT_POPULAR = """ball in hand
scratch on the break
foul
timeout
coaching
forfeit
lag"""

MAX_POPULAR = 12


def unknown_apa_links(body: str) -> list[str]:
    """The [apa: …] names in a text that don't match a page of the official APA rules."""
    return [m.group(1) or "(blank)" for m in APA_LINK.finditer(body or "") if apa_rules.find(m.group(1)) is None]


def check_apa_links(body: str) -> None:
    """Refuse a text with an [apa: …] link that would go nowhere."""
    bad = unknown_apa_links(body)
    if bad:
        names = ", ".join(f"[apa: {b}]" for b in bad)
        raise ValueError(f"Not saved: {names} doesn't match a page of the official APA rules. "
                         "Use the page's title as it shows in the Official APA rules list on the Rules page.")


def parse_shorthand(text: str) -> list[list[str]]:
    """'BIH = ball in hand, bih' lines -> [['bih', 'ball in hand'], ...]; raises ValueError on a bad line."""
    groups = []
    for n, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        key, sep, rest = line.partition("=")
        words = [w.strip().lower() for w in [key, *rest.split(",")] if w.strip()]
        if not sep or len(words) < 2:
            raise ValueError(f"Shorthand line {n} (“{line}”) needs a word, an = sign, then what it means, "
                             "e.g. BIH = ball in hand")
        groups.append(list(dict.fromkeys(words)))
    return groups


def parse_popular(text: str) -> list[str]:
    searches = [s.strip() for s in (text or "").splitlines() if s.strip()]
    if len(searches) > MAX_POPULAR:
        raise ValueError(f"Keep popular searches to {MAX_POPULAR} or fewer, so they fit under the search box.")
    return searches


class SearchSettings:
    """The director's shorthand list, popular searches, and the last APA link check."""

    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "rules_search.json"
        self.lock = threading.RLock()

    def load(self) -> dict:
        data = {"shorthand": DEFAULT_SHORTHAND, "popular": DEFAULT_POPULAR, "link_check": None}
        if self.path.exists():
            data.update(json.loads(self.path.read_text()))
        return data

    def _save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.path)

    def save_text(self, shorthand: str, popular: str) -> None:
        shorthand, popular = shorthand.replace("\r\n", "\n").strip(), popular.replace("\r\n", "\n").strip()
        parse_shorthand(shorthand)
        parse_popular(popular)
        with self.lock:
            data = self.load()
            data["shorthand"], data["popular"] = shorthand, popular
            self._save(data)

    def record_check(self, result: dict) -> None:
        with self.lock:
            data = self.load()
            data["link_check"] = result
            self._save(data)

    def check_is_stale(self, days: int = 7) -> bool:
        check = self.load()["link_check"]
        if not check:
            return True
        return (datetime.now() - datetime.fromisoformat(check["at"])).days >= days


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
        check_apa_links(body)
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


def _apa_link(m: re.Match) -> str:
    page = apa_rules.find(unescape(m.group(1)))
    if page is None:  # saving refuses these; an old one shows as plain text, never a broken link
        return m.group(0)
    return (f'<a class="apa-link" href="{escape(page["url"], quote=True)}" target="_blank" rel="noopener">'
            f'{escape(page["title"])}<span class="apa-badge">APA</span></a>')


def _inline(text: str) -> str:
    """Escape text, then turn **words** into bold and [apa: …] into links."""
    return APA_LINK.sub(_apa_link, re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escape(text)))


def _plain(text: str) -> str:
    """Heading text without the **bold** or [apa: …] marks, for section names and link addresses."""
    return APA_LINK.sub(r"\1", re.sub(r"\*\*(.+?)\*\*", r"\1", text)).strip()


def render(body: str, prefix: str = "rules") -> str:
    """Turn the director's plain text into safe HTML blocks (each one searchable).

    Every heading gets its own web address (#<prefix>--<heading>) and a link button, so a
    section can be shared on its own. Every block carries the name of the section it sits
    in (data-section), so a search result can show where it came from.
    """
    out: list[str] = []
    list_tag = None
    h3 = h4 = ""
    used: set[str] = set()

    def close_list():
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    def anchor(text: str) -> str:
        base = f"{prefix}--{slugify(_plain(text))}"
        anchor_id, n = base, 2
        while anchor_id in used:
            anchor_id, n = f"{base}-{n}", n + 1
        used.add(anchor_id)
        return anchor_id

    def section() -> str:
        return escape(" › ".join(p for p in (h3, h4) if p), quote=True)

    def heading(tag: str, text: str) -> str:
        anchor_id = anchor(text)
        return (f'<{tag} class="rule-block rule-head" id="{anchor_id}" data-section="{section()}">'
                f'{_inline(text)}<a class="rule-link" href="#{anchor_id}" title="Link to this section"'
                f' aria-label="Link to {escape(_plain(text), quote=True)}">#</a></{tag}>')

    def block(tag: str, text: str) -> str:
        return f'<{tag} class="rule-block" data-section="{section()}">{_inline(text)}</{tag}>'

    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line:
            close_list()
            continue
        if line.startswith("## "):
            close_list()
            h4 = _plain(line[3:])
            out.append(heading("h4", line[3:]))
        elif line.startswith("# "):
            close_list()
            h3, h4 = _plain(line[2:]), ""
            out.append(heading("h3", line[2:]))
        elif re.match(r"^[-*•]\s+", line):
            if list_tag != "ul":
                close_list()
                out.append("<ul>")
                list_tag = "ul"
            out.append(block("li", re.sub(r"^[-*•]\s+", "", line)))
        elif re.match(r"^\d+[.)]\s+", line):
            if list_tag != "ol":
                close_list()
                out.append("<ol>")
                list_tag = "ol"
            out.append(block("li", re.sub(r"^\d+[.)]\s+", "", line)))
        else:
            close_list()
            out.append(block("p", line))
    close_list()
    return "\n".join(out)
