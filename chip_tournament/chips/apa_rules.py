"""The official APA rules site (rules.poolplayers.com), as a list of page links.

Control Table never copies APA's rule text. It keeps each page's title, section and
web address, plus our own search words, so the Rules page can search them and send a
player straight to the right page on APA's site. The list lives in apa_rules.json.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

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


def _key(name: str) -> str:
    return " ".join(name.replace("®", "").lower().split())


def find(name: str) -> dict | None:
    """The page whose title is this name (ignoring case, spacing and ®), or None."""
    key = _key(name or "")
    return next((p for p in pages() if _key(p["title"]) == key), None) if key else None


def _fetch(url: str) -> tuple[int, str]:
    """Open a page the way a browser would; returns (status, the address it ended up at)."""
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Control Table rules link check)"})
    try:
        with urlopen(req, timeout=12) as resp:
            return resp.status, resp.geturl()
    except HTTPError as exc:
        return exc.code, url


def check_links(fetch=_fetch) -> dict:
    """Open every page on the list. A page passes if it opens (200) at the same address."""
    def one(page):
        try:
            status, final = fetch(page["url"])
        except Exception as exc:  # no answer at all: timeout, DNS, refused
            return {**page, "problem": f"didn't answer ({type(exc).__name__})"}
        if status != 200:
            return {**page, "problem": f"answered {status}"}
        if final.rstrip("/") != page["url"].rstrip("/"):
            return {**page, "problem": f"moved to {final}"}
        return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        bad = [r for r in pool.map(one, pages()) if r]
    return {"at": datetime.now().isoformat(timespec="seconds"), "total": len(pages()),
            "bad": [{"title": b["title"], "url": b["url"], "problem": b["problem"]} for b in bad]}


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
