"""Login, required only when bound beyond localhost (R17).

One DM, one password: a signed cookie is all the state needed. The signing
key lives in app.db so a restart does not log everyone out.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from .jobs import JobQueue

COOKIE = "scribe_session"
TTL = 30 * 86400


def _key(q: JobQueue) -> bytes:
    k = q.get_setting("cookie_key")
    if not k:
        k = secrets.token_hex(32)
        q.set_setting("cookie_key", k)
    return bytes.fromhex(k)


def make_token(q: JobQueue) -> str:
    exp = str(int(time.time()) + TTL)
    sig = hmac.new(_key(q), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def valid_token(q: JobQueue, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    good = hmac.new(_key(q), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > time.time()


def check_password(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode(), expected.encode())
