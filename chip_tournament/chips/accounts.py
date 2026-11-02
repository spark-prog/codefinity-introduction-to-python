"""Player login by emailed code.

A player types their member number (8 digits starting with 322) or their email. If the player
sheet has an email for them, a 6-digit code is emailed; typing it in logs this browser in for
90 days. Codes expire after 10 minutes, allow 5 tries, and are stored only as a keyed hash.
"Log out on all devices" bumps the player's version, which ends every other login at once.

Accounts live next to the player store (data/players/accounts.json), never in a tournament.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path

CODE_MINUTES = 10
CODE_TRIES = 5
LOGIN_DAYS = 90
RESEND_SECONDS = 60  # a new code at most once a minute per player


class LoginError(Exception):
    pass


def mask_email(email: str) -> str:
    """'annlee@gmail.com' -> 'a•••••@gmail.com' (enough to know which inbox to check)."""
    name, _, domain = (email or "").partition("@")
    return f"{name[:1]}{'•' * max(3, len(name) - 1)}@{domain}" if domain else ""


class Accounts:
    def __init__(self, root: str | Path, secret: str):
        self.path = Path(root) / "players" / "accounts.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.secret = secret.encode()
        self.lock = threading.RLock()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"codes": {}, "players": {}}

    def _save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.path)

    def _hash(self, member: str, code: str) -> str:
        return hmac.new(self.secret, f"{member}:{code}".encode(), hashlib.sha256).hexdigest()

    # codes -----------------------------------------------------------------
    def new_code(self, member: str, at: datetime | None = None) -> str:
        """Make (and remember) a fresh code for this player; the old one stops working."""
        at = at or datetime.now()
        with self.lock:
            data = self._load()
            old = data["codes"].get(member)
            if old and (at - datetime.fromisoformat(old["sent"])).total_seconds() < RESEND_SECONDS:
                raise LoginError("A code was just sent. Check your email (and spam), or wait a minute to get a new one.")
            code = f"{secrets.randbelow(1_000_000):06d}"
            data["codes"][member] = {"hash": self._hash(member, code), "sent": at.isoformat(timespec="seconds"),
                                     "expires": (at + timedelta(minutes=CODE_MINUTES)).isoformat(timespec="seconds"),
                                     "tries": 0}
            # Forget codes nobody used, so the file doesn't grow.
            data["codes"] = {m: c for m, c in data["codes"].items() if datetime.fromisoformat(c["expires"]) > at}
            self._save(data)
            return code

    def check_code(self, member: str, code: str, at: datetime | None = None) -> None:
        """Raise LoginError unless `code` is this player's current code. A right code can't be used twice."""
        at = at or datetime.now()
        code = re.sub(r"\D", "", code or "")
        with self.lock:
            data = self._load()
            c = data["codes"].get(member)
            if not c:
                raise LoginError("That code isn't valid any more. Ask for a new one.")
            if datetime.fromisoformat(c["expires"]) <= at:
                data["codes"].pop(member)
                self._save(data)
                raise LoginError("That code has expired. Ask for a new one.")
            if not hmac.compare_digest(c["hash"], self._hash(member, code)):
                c["tries"] += 1
                if c["tries"] >= CODE_TRIES:
                    data["codes"].pop(member)
                    self._save(data)
                    raise LoginError("Too many wrong codes. Ask for a new one.")
                self._save(data)
                left = CODE_TRIES - c["tries"]
                raise LoginError(f"That code isn't right. {left} more {'try' if left == 1 else 'tries'}.")
            data["codes"].pop(member)
            p = data["players"].setdefault(member, {"version": 1})
            p["last_login"] = at.isoformat(timespec="seconds")
            self._save(data)

    # logins ----------------------------------------------------------------
    def version(self, member: str) -> int:
        return self._load()["players"].get(member, {}).get("version", 1)

    def log_out_everywhere(self, member: str) -> None:
        with self.lock:
            data = self._load()
            p = data["players"].setdefault(member, {"version": 1})
            p["version"] = p.get("version", 1) + 1
            self._save(data)
