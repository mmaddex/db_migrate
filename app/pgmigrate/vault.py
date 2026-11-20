"""Connection strings at rest, and keeping them out of everything we show."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
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


def scram_verifier(password: str, salt: bytes | None = None, iterations: int = 4096) -> str:
    """Postgres's SCRAM-SHA-256 verifier for password, the form it stores in
    pg_authid. CREATE ROLE ... PASSWORD '<verifier>' stores it as given, so
    the password itself never reaches the server or its logs. The password
    must be ASCII (SASLprep leaves it unchanged)."""
    salt = salt if salt is not None else os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()
    def b64(b: bytes) -> str:
        return base64.b64encode(b).decode()
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(hashlib.sha256(client_key).digest())}:{b64(server_key)}"


def new_superuser_secret() -> dict:
    """The password for the Render superuser preflight creates
    (migration_super_user), and its SCRAM verifier. Kept with the migration's
    other secrets; only the app and the migration's sandbox ever see them."""
    password = secrets.token_urlsafe(32)
    return {"render_superuser_password": password, "render_superuser_verifier": scram_verifier(password)}


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
