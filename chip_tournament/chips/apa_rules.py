"""The official APA rules site (rules.poolplayers.com), as a list of page links.

Control Table never copies APA's rule text. It keeps each page's title, section and
web address, plus our own search words, so the Rules page can search them and send a
player straight to the right page on APA's site. The list lives in apa_rules.json.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

LIST_PATH = Path(__file__).with_name("apa_rules.json")
SITE = "https://rules.poolplayers.com/"


@lru_cache(maxsize=1)
def load() -> dict:
    """The whole list: {"site", "verified", "pages": [{title, section, url, otm, words}, ...]}."""
    data = json.loads(LIST_PATH.read_text())
    problems = check(data["pages"])
    if problems:
        raise ValueError("apa_rules.json: " + "; ".join(problems))
    return data


def pages() -> list[dict]:
    return load()["pages"]


def sections() -> list[tuple[str, list[dict]]]:
    """Pages grouped by section, in the order APA's own menu shows them."""
    groups: dict[str, list[dict]] = {}
    for page in pages():
        groups.setdefault(page["section"], []).append(page)
    return list(groups.items())


def check(pages: list[dict]) -> list[str]:
    """Problems that would put a bad link in front of players. Empty means the list is good."""
    problems, seen = [], set()
    for i, page in enumerate(pages, 1):
        where = f"page {i} ({page.get('title') or 'no title'})"
        if not page.get("title", "").strip() or not page.get("section", "").strip():
            problems.append(f"{where} needs a title and a section")
        url = page.get("url", "")
        if not url.startswith(SITE) or not url.endswith("/"):
            problems.append(f"{where} must link to a page on {SITE}")
        if url in seen:
            problems.append(f"{where} is listed twice")
        seen.add(url)
    return problems
