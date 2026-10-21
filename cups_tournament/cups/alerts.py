"""Text message alerts.

If TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER are set, texts
are sent through Twilio. Otherwise they are only logged, so the app still works
(and you can see what would have been sent) without an SMS account.
"""

from __future__ import annotations

import base64
import json
import os
import re
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


class SmsSender:
    def __init__(self, log_path: str | os.PathLike):
        self.log_path = Path(log_path)
        self.sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
        self.token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        self.from_number = os.environ.get("TWILIO_FROM_NUMBER", "")

    @property
    def enabled(self) -> bool:
        return bool(self.sid and self.token and self.from_number)

    def send(self, to: str, body: str) -> str:
        to = normalize_phone(to)
        if not to:
            status = "skipped: no number"
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
