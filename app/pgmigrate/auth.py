"""Per-request authentication: verify the access token Render's service
authentication forwards, against any OpenID Connect / OAuth 2.0 provider.

Every request except /healthz must carry a JWT in AUTH_TOKEN_HEADER
(default X-Forwarded-Access-Token). The token's signature is checked against
the issuer's published keys, along with its expiry, issuer and, when
configured, audience. Being on Render isn't enough: requests that arrive
over the private network, or with the platform's login switched off, are
rejected unless they carry a valid token.

Settings (environment):
  AUTH_ISSUER         required, e.g. https://render.okta.com, https://acme.us.auth0.com/,
                      https://login.microsoftonline.com/<tenant>/v2.0, https://kc.example.com/realms/x
  AUTH_JWKS_URL       optional; default: jwks_uri from <issuer>/.well-known/openid-configuration
  AUTH_AUDIENCE       optional; when set, the token's aud must contain it
  AUTH_ALGORITHMS     default RS256 (asymmetric only: RS/PS/ES/EdDSA)
  AUTH_TOKEN_HEADER   default X-Forwarded-Access-Token
  AUTH_USER_CLAIM     default sub (the claim that names the user, e.g. email)
  AUTH_ALLOWED_USERS  optional comma list of users or @domains allowed in
  AUTH_USERINFO_URL   optional; default: userinfo_endpoint from discovery (used only
                      to show the user's name)

The token must be a JWT. Providers that hand out opaque access tokens need
to forward an ID token or a JWT access token instead.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx
import jwt

log = logging.getLogger("pgmigrate.auth")

ASYMMETRIC = {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
KEYS_TTL = 600.0          # refresh the key set every 10 minutes
MIN_REFRESH = 30.0        # unknown kid: refetch at most this often
LEEWAY = 60               # clock skew allowance, seconds


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class AuthSettings:
    issuer: str
    jwks_url: str = ""
    audience: str = ""
    algorithms: tuple[str, ...] = ("RS256",)
    token_header: str = "X-Forwarded-Access-Token"
    user_claim: str = "sub"
    allowed_users: tuple[str, ...] = ()
    userinfo_url: str = ""

    def __post_init__(self):
        if not self.issuer:
            raise ValueError("AUTH_ISSUER is required")
        bad = [a for a in self.algorithms if a not in ASYMMETRIC]
        if bad or not self.algorithms:
            raise ValueError(f"AUTH_ALGORITHMS must be asymmetric ({', '.join(sorted(ASYMMETRIC))}); got {', '.join(bad) or 'none'}")


@dataclass
class _Keys:
    by_kid: dict[str, jwt.PyJWK] = field(default_factory=dict)
    unnamed: list[jwt.PyJWK] = field(default_factory=list)
    fetched_at: float = 0.0


class Authenticator:
    def __init__(self, settings: AuthSettings, *, http: httpx.AsyncClient | None = None,
                 static_jwks: dict | None = None):
        self.s = settings
        self._http = http
        self._static = static_jwks
        self._keys = _Keys()
        self._jwks_url = settings.jwks_url
        self._userinfo_url = settings.userinfo_url
        self._discovered = False
        self._lock = asyncio.Lock()
        self._names: dict[str, tuple[float, dict]] = {}

    @property
    def header(self) -> str:
        return self.s.token_header

    # ------------------------------------------------------------- keys
    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=10)
        return self._http

    async def _discover(self) -> None:
        if self._discovered or (self._jwks_url and self._userinfo_url) or self._static is not None:
            return
        url = self.s.issuer.rstrip("/") + "/.well-known/openid-configuration"
        r = await (await self._client()).get(url)
        r.raise_for_status()
        doc = r.json()
        if doc.get("issuer", "").rstrip("/") != self.s.issuer.rstrip("/"):
            raise AuthError("the issuer's discovery document names a different issuer", 503)
        self._jwks_url = self._jwks_url or doc.get("jwks_uri", "")
        self._userinfo_url = self._userinfo_url or doc.get("userinfo_endpoint", "")
        self._discovered = True
        if not self._jwks_url:
            raise AuthError("no jwks_uri in the issuer's discovery document; set AUTH_JWKS_URL", 503)

    async def _load(self, *, force: bool = False) -> None:
        """Fetch the key set when it's missing or stale. force (an unknown kid)
        refetches early, but at most once per MIN_REFRESH so bad tokens can't
        hammer the provider."""
        async with self._lock:
            have = self._keys.fetched_at > 0
            age = time.monotonic() - self._keys.fetched_at
            if have and not force and age < KEYS_TTL:
                return
            if have and force and age < MIN_REFRESH:
                return
            if self._static is not None:
                jwks = self._static
            else:
                await self._discover()
                r = await (await self._client()).get(self._jwks_url)
                r.raise_for_status()
                jwks = r.json()
            keys = _Keys(fetched_at=time.monotonic())
            for raw in jwks.get("keys", []):
                if raw.get("use", "sig") != "sig":
                    continue
                try:
                    k = jwt.PyJWK(raw)
                except jwt.PyJWTError:
                    continue  # key types this library can't use
                if raw.get("kid"):
                    keys.by_kid[raw["kid"]] = k
                else:
                    keys.unnamed.append(k)
            self._keys = keys
            log.info("loaded %d signing key(s) for %s", len(keys.by_kid) + len(keys.unnamed), self.s.issuer)

    async def _candidates(self, kid: str | None) -> list[jwt.PyJWK]:
        try:
            await self._load()
        except (httpx.HTTPError, ValueError, AuthError) as e:
            log.warning("could not load signing keys: %s", e)
            if not self._keys.fetched_at:
                raise AuthError("can't load the identity provider's signing keys", 503) from e
        if kid:
            if kid not in self._keys.by_kid:
                try:
                    await self._load(force=True)  # key rotation
                except (httpx.HTTPError, ValueError, AuthError) as e:
                    log.warning("could not refresh signing keys: %s", e)
            key = self._keys.by_kid.get(kid)
            return [key] if key else []
        return [*self._keys.unnamed, *self._keys.by_kid.values()]

    # ----------------------------------------------------------- verify
    async def verify(self, raw: str | None) -> dict:
        """Return the token's claims, or raise AuthError."""
        if not raw:
            raise AuthError(f"missing {self.s.token_header} header")
        token = raw[7:].strip() if raw[:7].lower() == "bearer " else raw.strip()
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as e:
            raise AuthError("malformed token") from e
        if header.get("alg") not in self.s.algorithms:
            raise AuthError("token algorithm not allowed")
        keys = await self._candidates(header.get("kid"))
        if not keys:
            raise AuthError("token signed with an unknown key")
        last: Exception | None = None
        for key in keys:
            try:
                claims = jwt.decode(
                    token, key.key, algorithms=list(self.s.algorithms), issuer=self.s.issuer,
                    audience=self.s.audience or None, leeway=LEEWAY,
                    options={"require": ["exp", "iss"], "verify_aud": bool(self.s.audience)})
                break
            except jwt.InvalidSignatureError as e:
                last = e
                continue
            except jwt.ExpiredSignatureError as e:
                raise AuthError("token expired") from e
            except jwt.InvalidIssuerError as e:
                raise AuthError("token from the wrong issuer") from e
            except jwt.InvalidAudienceError as e:
                raise AuthError("token for a different audience") from e
            except jwt.PyJWTError as e:
                raise AuthError(f"invalid token: {e}") from e
        else:
            raise AuthError("invalid token signature") from last
        user = claims.get(self.s.user_claim)
        if not isinstance(user, str) or not user:
            raise AuthError(f"token has no {self.s.user_claim} claim")
        if self.s.allowed_users and not self._allowed(user):
            raise AuthError(f"{user} is not allowed to use this app", 403)
        return claims

    def _allowed(self, user: str) -> bool:
        u = user.lower()
        for rule in self.s.allowed_users:
            r = rule.lower()
            if (r.startswith("@") and u.endswith(r)) or u == r:
                return True
        return False

    def user_of(self, claims: dict) -> str:
        return claims[self.s.user_claim]

    async def display_name(self, raw_token: str, claims: dict) -> str | None:
        """The user's name from the provider's userinfo endpoint (cached, best effort)."""
        user = self.user_of(claims)
        hit = self._names.get(user)
        if hit and time.monotonic() - hit[0] < 3600:
            return hit[1].get("name")
        if claims.get("name"):
            return claims["name"]
        try:
            await self._discover()
        except (httpx.HTTPError, AuthError, ValueError):
            return None
        if not self._userinfo_url:
            return None
        token = raw_token[7:].strip() if raw_token[:7].lower() == "bearer " else raw_token
        try:
            r = await (await self._client()).get(self._userinfo_url, headers={"Authorization": f"Bearer {token}"})
            info = r.json() if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            info = {}
        self._names[user] = (time.monotonic(), info)
        return info.get("name")


def settings_from_env(get) -> AuthSettings | None:
    """AuthSettings from environment lookups, or None when AUTH_ISSUER is unset."""
    issuer = get("AUTH_ISSUER")
    if not issuer:
        return None
    split = lambda v: tuple(x.strip() for x in v.split(",") if x.strip())  # noqa: E731
    return AuthSettings(
        issuer=issuer, jwks_url=get("AUTH_JWKS_URL"), audience=get("AUTH_AUDIENCE"),
        algorithms=split(get("AUTH_ALGORITHMS") or "RS256"),
        token_header=get("AUTH_TOKEN_HEADER") or "X-Forwarded-Access-Token",
        user_claim=get("AUTH_USER_CLAIM") or "sub", allowed_users=split(get("AUTH_ALLOWED_USERS")),
        userinfo_url=get("AUTH_USERINFO_URL"))
