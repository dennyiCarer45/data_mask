"""HMAC proof that the public gateway completed local validation. No original values."""

import hashlib
import hmac
import json
import secrets
import threading
import time

from .errors import PrivacyError

FIELDS = {"model", "messages", "max_tokens", "stream"}


def canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sign(payload, key: str, now=None):
    if len(key) < 32 or set(payload) != FIELDS:
        raise PrivacyError("approval_configuration_invalid", 503)
    ticket = {"time": int(time.time() if now is None else now), "nonce": secrets.token_hex(16)}
    ticket["signature"] = hmac.new(
        key.encode(), canonical({**ticket, "payload": payload}), hashlib.sha256
    ).hexdigest()
    return ticket


class ApprovalVerifier:
    def __init__(self, key: str):
        self.key = key
        self.used = {}
        self.lock = threading.Lock()

    def verify(self, payload, ticket, now=None):
        try:
            current = int(time.time() if now is None else now)
            if len(self.key) < 32 or set(payload) != FIELDS:
                raise ValueError()
            if set(ticket) != {"time", "nonce", "signature"}:
                raise ValueError()
            if type(ticket["time"]) is not int or abs(current - ticket["time"]) > 30:
                raise ValueError()
            if not isinstance(ticket["nonce"], str) or len(ticket["nonce"]) != 32:
                raise ValueError()
            expected = hmac.new(
                self.key.encode(),
                canonical({"time": ticket["time"], "nonce": ticket["nonce"], "payload": payload}),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected, ticket["signature"]):
                raise ValueError()
            with self.lock:
                self.used = {n: t for n, t in self.used.items() if current - t <= 60}
                if ticket["nonce"] in self.used or len(self.used) >= 10000:
                    raise ValueError()
                self.used[ticket["nonce"]] = current
        except Exception:
            raise PrivacyError("privacy_approval_required", 403) from None
