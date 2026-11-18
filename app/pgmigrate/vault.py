"""Connection strings at rest, and keeping them out of everything we show."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from urllib.parse import unquote, urlsplit

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    """Encrypts the secret part of a migration (URLs, webhook) with a key
    derived from APP_SECRET_KEY."""

    def __init__(self, secret_key: str):
        if not secret_key:
            raise ValueError("APP_SECRET_KEY is not set")
        key = base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest())
        self._fernet = Fernet(key)

    def seal(self, data: dict) -> bytes:
        return self._fernet.encrypt(json.dumps(data).encode())

    def open(self, blob: bytes) -> dict:
        try:
            return json.loads(self._fernet.decrypt(bytes(blob)))
        except InvalidToken as e:
            raise ValueError("cannot decrypt stored secrets: was APP_SECRET_KEY changed?") from e


_RAW_PASSWORD = re.compile(r"^[A-Za-z]+://[^:/@]*:([^@]*)@")


def secret_strings(*values: str) -> list[str]:
    """Strings to scrub from output: each value, and for URLs the password
    as written and decoded. Longest first so a URL is replaced before its
    password."""
    out: set[str] = set()
    for v in values:
        if not v:
            continue
        out.add(v)
        m = _RAW_PASSWORD.match(v)
        if m and m.group(1):
            out.add(m.group(1))
            out.add(unquote(m.group(1)))
    return sorted((s for s in out if len(s) >= 3), key=len, reverse=True)


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        text = text.replace(s, "****")
    return text


def summarize(url: str) -> dict:
    """user@host:port/db with the password removed."""
    if not url:
        return {"display": "", "host": "", "db": ""}
    u = urlsplit(url)
    user = f"{unquote(u.username)}@" if u.username else ""
    port = f":{u.port}" if u.port else ""
    db = u.path.strip("/")
    return {"display": f"{u.scheme}://{user}{u.hostname}{port}/{db}", "host": u.hostname or "", "db": db}
