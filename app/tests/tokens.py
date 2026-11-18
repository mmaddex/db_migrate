"""A test identity provider: RSA signing keys, their JWKS, and token minting."""

from __future__ import annotations

import json
import time

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from pgmigrate.auth import Authenticator, AuthSettings

ISSUER = "https://idp.example.test"
HEADER = "X-Forwarded-Access-Token"


class TestIdP:
    __test__ = False  # not a pytest class

    def __init__(self, kid: str = "k1"):
        self.kid = kid
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwk(self) -> dict:
        d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        d.update(kid=self.kid, use="sig", alg="RS256")
        return d

    def jwks(self) -> dict:
        return {"keys": [self.jwk()]}

    def mint(self, sub: str = "alice@example.test", *, iss: str = ISSUER, exp_in: int = 600,
             kid: str | None = None, key=None, alg: str = "RS256", **claims) -> str:
        now = int(time.time())
        body = {"sub": sub, "iss": iss, "iat": now, "exp": now + exp_in, **claims}
        return jwt.encode(body, key or self.key, algorithm=alg, headers={"kid": kid or self.kid})


IDP = TestIdP()


def authenticator(**settings) -> Authenticator:
    return Authenticator(AuthSettings(issuer=ISSUER, **settings), static_jwks=IDP.jwks())


def auth_headers(sub: str = "alice@example.test", **claims) -> dict:
    return {HEADER: IDP.mint(sub, exp_in=3600, **claims)}
