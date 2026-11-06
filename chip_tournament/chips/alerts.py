"""Text message alerts.

If TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER are set, texts
are sent through Twilio. Otherwise they are only logged, so the app still works
(and you can see what would have been sent) without an SMS account.

Opt-outs: a number that replies STOP (or that Twilio reports as opted out, error 21610) goes on
one league-wide list (data/sms_optouts.json) and is never texted again by any event until it
replies START. Twilio itself also blocks those numbers; the list means the app knows too.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path


def normalize_phone(number: str) -> str:
    """Turn '(555) 123-4567' into '+15551234567' (US numbers by default)."""
    digits = re.sub(r"\D", "", number or "")
    if not digits:
        return ""
    if number.strip().startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits


STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "optout", "opt-out", "revoke"}
START_WORDS = {"start", "unstop", "yes", "optin", "opt-in"}
TWILIO_OPTED_OUT = 21610  # Twilio's error code: "Attempt to send to unsubscribed recipient"


class OptOuts:
    """Numbers that asked not to be texted, league-wide: {"+19045550123": {"at", "how"}}."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.lock = threading.RLock()

    def all(self) -> dict[str, dict]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def blocked(self, number: str) -> bool:
        return normalize_phone(number) in self.all()

    def _save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.path)

    def add(self, number: str, how: str) -> None:
        number = normalize_phone(number)
        if not number:
            return
        with self.lock:
            data = self.all()
            if number not in data:
                data[number] = {"at": datetime.now().isoformat(timespec="seconds"), "how": how}
                self._save(data)

    def remove(self, number: str) -> bool:
        number = normalize_phone(number)
        with self.lock:
            data = self.all()
            if data.pop(number, None) is None:
                return False
            self._save(data)
            return True

    def handle_reply(self, number: str, body: str) -> str:
        """A text a player sent us: STOP words opt out, START words opt back in. Returns 'stop', 'start' or ''."""
        word = (body or "").strip().lower().rstrip(".!")
        if word in STOP_WORDS:
            self.add(number, f"replied {word.upper()}")
            return "stop"
        if word in START_WORDS:
            self.remove(number)
            return "start"
        return ""


def valid_twilio_signature(token: str, url: str, params: dict, signature: str) -> bool:
    """Twilio signs each webhook: base64(HMAC-SHA1(auth token, url + every param name and value, sorted))."""
    if not (token and signature):
        return False
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = base64.b64encode(hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(digest, signature)


class SmsSender:
    def __init__(self, log_path: str | os.PathLike, optouts: OptOuts | None = None):
        self.log_path = Path(log_path)
        self.optouts = optouts
        self.sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
        self.token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        self.from_number = os.environ.get("TWILIO_FROM_NUMBER", "")

    @property
    def enabled(self) -> bool:
        return bool(self.sid and self.token and self.from_number)

    def send(self, to: str, body: str, test: bool = False) -> str:
        to = normalize_phone(to)
        if not to:
            status = "skipped: no number"
        elif self.optouts is not None and self.optouts.blocked(to):
            status = "not sent: opted out (replied STOP)"
        elif test:
            status = "not sent (test event)"
        elif not self.enabled:
            status = "logged (SMS not configured)"
        else:
            status = self._send_twilio(to, body)
        self._log(to, body, status)
        return status

    def _send_twilio(self, to: str, body: str) -> str:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json"
        data = urllib.parse.urlencode({"To": to, "From": self.from_number, "Body": body}).encode()
        auth = base64.b64encode(f"{self.sid}:{self.token}".encode()).decode()
        req = urllib.request.Request(url, data=data, headers={"Authorization": f"Basic {auth}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return f"sent ({resp.status})"
        except urllib.error.HTTPError as exc:
            try:
                code = json.loads(exc.read().decode() or "{}").get("code")
            except Exception:
                code = None
            if code == TWILIO_OPTED_OUT and self.optouts is not None:
                self.optouts.add(to, "Twilio reported the number opted out")
                return "not sent: opted out (replied STOP)"
            return f"failed: {exc}"
        except Exception as exc:  # a failed text must never break scoring
            return f"failed: {exc}"

    def _log(self, to: str, body: str, status: str) -> None:
        entry = {"at": datetime.now().isoformat(timespec="seconds"), "to": to, "body": body, "status": status}
        with self.log_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    def recent(self, limit: int = 100) -> list[dict]:
        if not self.log_path.exists():
            return []
        lines = self.log_path.read_text().splitlines()[-limit:]
        return [json.loads(line) for line in reversed(lines)]
