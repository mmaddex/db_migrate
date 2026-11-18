"""Per-request authentication: verify the access token Render's service
authentication forwards, against any OpenID Connect / OAuth 2.0 provider.

Every request except /healthz must carry an access token in
AUTH_TOKEN_HEADER (default X-Forwarded-Access-Token). Being on Render isn't
enough: requests that arrive over the private network, or with the
platform's login switched off, are rejected unless they carry a valid token.
There are two ways to check it (AUTH_VERIFY):

  jwt       (default) The token must be a JWT. Its signature is checked
            against the issuer's published keys, along with its expiry,
            issuer and, when configured, audience. Okta's org authorization
            server and Entra ID send JWTs.
  userinfo  The token is sent to the issuer's userinfo endpoint, which says
            whether it is valid and who it belongs to. This works with access
            tokens that aren't JWTs, such as Auth0's when no API audience is
            requested. Answers are cached for USERINFO_TTL seconds per token.

Settings (environment):
  AUTH_ISSUER         required, e.g. https://render.okta.com, https://acme.us.auth0.com/,
                      https://login.microsoftonline.com/<tenant>/v2.0, https://kc.example.com/realms/x
  AUTH_VERIFY         jwt (default) or userinfo
  AUTH_JWKS_URL       optional; default: jwks_uri from <issuer>/.well-known/openid-configuration
  AUTH_AUDIENCE       optional, jwt only; when set, the token's aud must contain it
  AUTH_ALGORITHMS     default RS256 (asymmetric only: RS/PS/ES/EdDSA)
  AUTH_TOKEN_HEADER   default X-Forwarded-Access-Token
  AUTH_USER_CLAIM     default sub (the claim that names the user, e.g. email)
  AUTH_ALLOWED_USERS  optional comma list of users or @domains allowed in
  AUTH_USERINFO_URL   optional; default: userinfo_endpoint from discovery

When AUTH_USER_CLAIM is email, an email the provider marks unverified is refused.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field

import httpx
import jwt

log = logging.getLogger("pgmigrate.auth")

ASYMMETRIC = {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
MODES = ("jwt", "userinfo")
KEYS_TTL = 600.0          # refresh the key set every 10 minutes
MIN_REFRESH = 30.0        # unknown kid: refetch at most this often
LEEWAY = 60               # clock skew allowance, seconds
USERINFO_TTL = 300.0      # how long a userinfo answer about a token is trusted
USERINFO_DENY_TTL = 30.0  # how long a rejected token stays rejected without asking again
USERINFO_CACHE_MAX = 1024
SIGNIN_LOG_EVERY = 3600.0  # log a user's sign-in at most this often


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
    verify: str = "jwt"

    def __post_init__(self):
        if not self.issuer:
            raise ValueError("AUTH_ISSUER is required")
        bad = [a for a in self.algorithms if a not in ASYMMETRIC]
        if bad or not self.algorithms:
            raise ValueError(f"AUTH_ALGORITHMS must be asymmetric ({', '.join(sorted(ASYMMETRIC))}); got {', '.join(bad) or 'none'}")
        if self.verify not in MODES:
            raise ValueError(f"AUTH_VERIFY must be one of {', '.join(MODES)}; got {self.verify!r}")
        if self.verify == "userinfo" and self.audience:
            raise ValueError("AUTH_AUDIENCE can't be checked with AUTH_VERIFY=userinfo: "
                             "the provider's userinfo answer doesn't say which audience a token is for")

    def describe(self) -> str:
        return (f"issuer={self.issuer} verify={self.verify} header={self.token_header} "
                f"user_claim={self.user_claim} audience={self.audience or '(not checked)'} "
                f"algorithms={','.join(self.algorithms)} "
                f"allowed_users={','.join(self.allowed_users) or '(anyone signed in)'}")


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
        # userinfo mode: token digest -> (expires, claims or AuthError), and
        # the one in-flight question per token while it's being asked.
        self._answers: dict[str, tuple[float, dict | AuthError]] = {}
        self._asking: dict[str, asyncio.Future] = {}
        self._seen: dict[str, float] = {}

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
        log.info("discovered %s: jwks_uri=%s userinfo_endpoint=%s", self.s.issuer,
                 self._jwks_url or "(none)", self._userinfo_url or "(none)")
        if not self._jwks_url and self.s.verify == "jwt":
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
        """Return the token's claims (or the userinfo answer), or raise AuthError."""
        token = _strip_bearer(raw)
        if not token:
            raise AuthError(f"missing {self.s.token_header} header")
        if self.s.verify == "userinfo":
            claims = await self._verify_userinfo(token)
        else:
            claims = await self._verify_jwt(token)
        user = claims.get(self.s.user_claim)
        if not isinstance(user, str) or not user:
            raise AuthError(f"token has no {self.s.user_claim} claim")
        if self.s.user_claim == "email" and claims.get("email_verified") is False:
            raise AuthError(f"{user} hasn't verified their email with the identity provider", 403)
        if self.s.allowed_users and not self._allowed(user):
            raise AuthError(f"{user} is not allowed to use this app", 403)
        now = time.monotonic()
        if now - self._seen.get(user, -SIGNIN_LOG_EVERY) >= SIGNIN_LOG_EVERY:
            self._seen[user] = now
            log.info("signed in: %s (token checked by %s)", user, self.s.verify)
        return claims

    async def _verify_jwt(self, token: str) -> dict:
        if token.count(".") != 2:
            raise AuthError("access token is not a JWT")
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
        return claims

    # --------------------------------------------------------- userinfo
    async def _verify_userinfo(self, token: str) -> dict:
        """Ask the issuer's userinfo endpoint about the token. One question per
        token at a time (a page load sends several requests at once), and the
        answer is reused for USERINFO_TTL seconds: providers rate-limit userinfo."""
        key = hashlib.sha256(token.encode()).hexdigest()
        hit = self._answers.get(key)
        if hit and hit[0] > time.monotonic():
            if isinstance(hit[1], AuthError):
                raise AuthError(str(hit[1]), hit[1].status)
            return dict(hit[1])
        pending = self._asking.get(key)
        if pending is not None:
            return dict(await asyncio.shield(pending))
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._asking[key] = fut
        try:
            claims = await self._ask_userinfo(token)
        except BaseException as e:
            err = e if isinstance(e, AuthError) else AuthError("checking the token was interrupted", 503)
            if err.status == 401:  # the provider said no: don't ask again for a while
                self._remember(key, err, USERINFO_DENY_TTL)
            fut.set_exception(err)
            fut.exception()  # retrieved: no "never retrieved" warning when nobody else waited
            raise
        else:
            self._remember(key, claims, USERINFO_TTL)
            fut.set_result(claims)
            return dict(claims)
        finally:
            self._asking.pop(key, None)

    def _remember(self, key: str, answer, ttl: float) -> None:
        now = time.monotonic()
        if len(self._answers) >= USERINFO_CACHE_MAX:
            for k in [k for k, (exp, _) in self._answers.items() if exp <= now]:
                del self._answers[k]
            while len(self._answers) >= USERINFO_CACHE_MAX:
                del self._answers[next(iter(self._answers))]
        self._answers[key] = (now + ttl, answer)

    async def _ask_userinfo(self, token: str) -> dict:
        try:
            await self._discover()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("could not read the issuer's discovery document: %s", e)
            raise AuthError("can't reach the identity provider to check the token", 503) from e
        if not self._userinfo_url:
            raise AuthError("no userinfo_endpoint in the issuer's discovery document; set AUTH_USERINFO_URL", 503)
        try:
            r = await (await self._client()).get(self._userinfo_url, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            log.warning("userinfo request failed: %s", e)
            raise AuthError("can't reach the identity provider to check the token", 503) from e
        if r.status_code in (401, 403):
            log.info("userinfo refused the token: HTTP %d %s", r.status_code,
                     _safe(r.headers.get("www-authenticate", "")))
            raise AuthError("the identity provider rejected the token")
        if r.status_code == 429:
            log.warning("userinfo is rate limiting token checks (HTTP 429)")
            raise AuthError("the identity provider is rate limiting token checks; try again shortly", 503)
        if r.status_code != 200:
            log.warning("userinfo answered HTTP %d", r.status_code)
            raise AuthError("the identity provider couldn't check the token", 503)
        try:
            claims = r.json()
        except ValueError as e:
            raise AuthError("the identity provider's userinfo answer isn't JSON", 503) from e
        if not isinstance(claims, dict) or not claims.get("sub"):
            raise AuthError("the identity provider's userinfo answer has no sub", 503)
        return claims

    # ---------------------------------------------------------- logging
    def describe_token(self, raw: str | None) -> str:
        """What kind of token a request carried, for logs. Never the token
        itself: only its shape and, for a JWT, unverified header and claims."""
        token = _strip_bearer(raw)
        if not token:
            return f"no {self.s.token_header} header"
        parts = token.split(".")
        if len(parts) == 3:
            head, body = _b64json(parts[0]), _b64json(parts[1])
            if head is None:
                return f"3-part token whose header isn't JSON, {len(token)} chars"
            out = [f"JWT alg={_safe(head.get('alg'))} kid={_safe(head.get('kid'))}"]
            if body is not None:
                out.append(f"iss={_safe(body.get('iss'))} aud={_safe(body.get('aud'))}")
                if isinstance(body.get("exp"), (int, float)):
                    out.append(f"expires_in={int(body['exp'] - time.time())}s")
            return " ".join(out)
        if len(parts) == 5:
            head = _b64json(parts[0]) or {}
            return (f"encrypted JWT (JWE) alg={_safe(head.get('alg'))} enc={_safe(head.get('enc'))} "
                    f"iss={_safe(head.get('iss'))}, {len(token)} chars: not readable by this app")
        return f"opaque token, {len(token)} chars"

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
        token = _strip_bearer(raw_token)
        try:
            r = await (await self._client()).get(self._userinfo_url, headers={"Authorization": f"Bearer {token}"})
            info = r.json() if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            info = {}
        self._names[user] = (time.monotonic(), info)
        return info.get("name")


def _strip_bearer(raw: str | None) -> str:
    parts = (raw or "").split(None, 1)
    if parts and parts[0].lower() == "bearer":
        return parts[1].strip() if len(parts) > 1 else ""
    return (raw or "").strip()


def _b64json(part: str) -> dict | None:
    try:
        v = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (ValueError, TypeError):
        return None
    return v if isinstance(v, dict) else None


def _safe(v, limit: int = 120) -> str:
    """A value from an unverified token or header, made safe to log."""
    if v is None:
        return "-"
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return "".join(c if c.isprintable() else "?" for c in s)[:limit]


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
        userinfo_url=get("AUTH_USERINFO_URL"), verify=(get("AUTH_VERIFY") or "jwt").strip().lower())
