"""Strict per-request authentication: token verification, key discovery and
rotation, and the app refusing anything without a valid token."""

import asyncio
import base64
import hashlib
import hmac
import json
import logging

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pgmigrate.auth import Authenticator, AuthError, AuthSettings, settings_from_env
from pgmigrate.web import create_app

from .fake_ops import FakeOps
from .test_app import make_cfg, running
from .tokens import HEADER, IDP, ISSUER, TestIdP, auth_headers, authenticator


# ------------------------------------------------------------- verification
async def test_valid_token_and_bearer_prefix():
    a = authenticator()
    claims = await a.verify(IDP.mint("alice@example.test"))
    assert claims["sub"] == "alice@example.test" and a.user_of(claims) == "alice@example.test"
    assert (await a.verify("Bearer " + IDP.mint()))["iss"] == ISSUER


@pytest.mark.parametrize("token,reason", [
    (None, "missing"),
    ("", "missing"),
    ("Bearer ", "missing"),
    ("not-a-jwt", "not a JWT"),
    ("a.b.c.d.e", "not a JWT"),      # JWE: encrypted, unreadable without the provider's key
    ("!!!.e30.sig", "malformed"),
])
async def test_rejects_missing_or_malformed(token, reason):
    with pytest.raises(AuthError, match=reason) as e:
        await authenticator().verify(token)
    assert e.value.status == 401


async def test_rejects_expired_wrong_issuer_wrong_key_unknown_kid():
    a = authenticator()
    with pytest.raises(AuthError, match="expired"):
        await a.verify(IDP.mint(exp_in=-3600))
    with pytest.raises(AuthError, match="wrong issuer"):
        await a.verify(IDP.mint(iss="https://evil.example"))
    impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AuthError, match="signature"):
        await a.verify(IDP.mint(key=impostor))  # same kid, different key
    with pytest.raises(AuthError, match="unknown key"):
        await a.verify(IDP.mint(kid="nope"))
    no_exp = jwt.encode({"sub": "a", "iss": ISSUER}, IDP.key, algorithm="RS256", headers={"kid": IDP.kid})
    with pytest.raises(AuthError):
        await a.verify(no_exp)


async def test_rejects_alg_none_and_hmac_confusion():
    a = authenticator()
    enc = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    body = enc(json.dumps({"sub": "a", "iss": ISSUER, "exp": 9999999999}).encode())
    unsigned = enc(json.dumps({"alg": "none", "kid": IDP.kid}).encode()) + "." + body + "."
    with pytest.raises(AuthError, match="algorithm"):
        await a.verify(unsigned)
    # Key confusion: HS256 "signed" with the provider's public key as the secret.
    pem = IDP.key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    head = enc(json.dumps({"alg": "HS256", "typ": "JWT", "kid": IDP.kid}).encode())
    sig = enc(hmac.new(pem, f"{head}.{body}".encode(), hashlib.sha256).digest())
    with pytest.raises(AuthError, match="algorithm"):
        await a.verify(f"{head}.{body}.{sig}")


async def test_audience_and_allowed_users_and_user_claim():
    a = authenticator(audience="api://pg-migrate")
    with pytest.raises(AuthError, match="audience|aud"):
        await a.verify(IDP.mint())
    with pytest.raises(AuthError, match="audience"):
        await a.verify(IDP.mint(aud="api://other"))
    assert (await a.verify(IDP.mint(aud="api://pg-migrate")))["aud"] == "api://pg-migrate"

    a = authenticator(allowed_users=("@example.test", "ops@partner.test"))
    await a.verify(IDP.mint("alice@example.test"))
    await a.verify(IDP.mint("ops@partner.test"))
    with pytest.raises(AuthError) as e:
        await a.verify(IDP.mint("mallory@evil.test"))
    assert e.value.status == 403

    a = authenticator(user_claim="email")
    with pytest.raises(AuthError, match="no email claim"):
        await a.verify(IDP.mint())
    assert a.user_of(await a.verify(IDP.mint(email="alice@example.test"))) == "alice@example.test"


def test_settings_reject_symmetric_algorithms_and_read_env():
    with pytest.raises(ValueError):
        AuthSettings(issuer=ISSUER, algorithms=("HS256",))
    with pytest.raises(ValueError):
        AuthSettings(issuer="")
    env = {"AUTH_ISSUER": ISSUER, "AUTH_ALGORITHMS": "RS256, ES256", "AUTH_ALLOWED_USERS": "@a.test, b@c.test"}
    s = settings_from_env(lambda k: env.get(k, ""))
    assert s.algorithms == ("RS256", "ES256") and s.allowed_users == ("@a.test", "b@c.test")
    assert s.token_header == "X-Forwarded-Access-Token" and s.user_claim == "sub" and s.verify == "jwt"
    assert settings_from_env(lambda k: "") is None
    env["AUTH_VERIFY"] = "UserInfo"
    assert settings_from_env(lambda k: env.get(k, "")).verify == "userinfo"
    with pytest.raises(ValueError, match="AUTH_VERIFY"):
        AuthSettings(issuer=ISSUER, verify="introspect")
    with pytest.raises(ValueError, match="AUTH_AUDIENCE"):
        AuthSettings(issuer=ISSUER, verify="userinfo", audience="api://pg-migrate")


# ------------------------------------------------- discovery and rotation
def provider(state: dict):
    """An OIDC provider over httpx.MockTransport. state['keys'] is served as the JWKS."""
    def handle(req: httpx.Request) -> httpx.Response:
        state.setdefault("hits", []).append(req.url.path)
        if req.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": state.get("issuer", ISSUER),
                                             "jwks_uri": ISSUER + "/oauth2/v1/keys",
                                             "userinfo_endpoint": ISSUER + "/oauth2/v1/userinfo"})
        if req.url.path == "/oauth2/v1/keys":
            return httpx.Response(200, json={"keys": state["keys"]})
        if req.url.path == "/oauth2/v1/userinfo":
            if "userinfo" in state:
                return state["userinfo"](req)
            return httpx.Response(200, json={"name": "Alice Example"})
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


# Opaque access tokens the fake provider knows, and what its userinfo says about them.
USERS = {
    "opaque-alice": {"sub": "auth0|alice", "email": "alice@example.test", "email_verified": True, "name": "Alice"},
    "opaque-unverified": {"sub": "auth0|eve", "email": "eve@example.test", "email_verified": False},
}


def userinfo(req: httpx.Request) -> httpx.Response:
    token = req.headers.get("authorization", "").removeprefix("Bearer ")
    if token == "opaque-busy":
        return httpx.Response(429)
    if token in USERS:
        return httpx.Response(200, json=USERS[token])
    return httpx.Response(401, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})


def userinfo_provider(state: dict | None = None) -> tuple[dict, httpx.AsyncClient]:
    state = {"keys": [], "userinfo": userinfo, **(state or {})}
    return state, provider(state)


async def test_discovers_keys_follows_rotation_and_loads_name(monkeypatch):
    import pgmigrate.auth as authmod
    monkeypatch.setattr(authmod, "MIN_REFRESH", 0.0)
    state = {"keys": [IDP.jwk()]}
    a = Authenticator(AuthSettings(issuer=ISSUER), http=provider(state))
    await a.verify(IDP.mint())
    assert state["hits"][:2] == ["/.well-known/openid-configuration", "/oauth2/v1/keys"]
    rotated = TestIdP(kid="k2")
    state["keys"] = [IDP.jwk(), rotated.jwk()]
    claims = await a.verify(rotated.mint("bob@example.test"))   # unknown kid -> refetch
    assert claims["sub"] == "bob@example.test" and state["hits"].count("/oauth2/v1/keys") == 2
    tok = rotated.mint("bob@example.test")
    assert await a.display_name(tok, await a.verify(tok)) == "Alice Example"


async def test_refuses_a_discovery_document_for_another_issuer():
    a = Authenticator(AuthSettings(issuer=ISSUER), http=provider({"keys": [], "issuer": "https://evil.example"}))
    with pytest.raises(AuthError) as e:
        await a.verify(IDP.mint())
    assert e.value.status == 503


# ------------------------------------------------------ userinfo checking
async def test_userinfo_checks_opaque_tokens_and_caches_the_answer():
    state, http = userinfo_provider()
    a = Authenticator(AuthSettings(issuer=ISSUER, verify="userinfo", user_claim="email"), http=http)
    claims = await a.verify("Bearer opaque-alice")
    assert a.user_of(claims) == "alice@example.test"
    assert await a.display_name("opaque-alice", claims) == "Alice"
    await a.verify("opaque-alice")
    assert state["hits"].count("/oauth2/v1/userinfo") == 1  # cached

    with pytest.raises(AuthError, match="rejected") as e:
        await a.verify("opaque-forged")
    assert e.value.status == 401
    with pytest.raises(AuthError, match="rejected"):
        await a.verify("opaque-forged")
    assert state["hits"].count("/oauth2/v1/userinfo") == 2  # a rejection is remembered briefly too

    with pytest.raises(AuthError, match="verified") as e:
        await a.verify("opaque-unverified")
    assert e.value.status == 403
    with pytest.raises(AuthError, match="rate limiting") as e:
        await a.verify("opaque-busy")
    assert e.value.status == 503


async def test_userinfo_asks_once_for_concurrent_requests():
    """A page load sends several requests with the same token at once; the
    provider must be asked once, not once per request."""
    hits, gate = [], asyncio.Event()

    async def handle(req: httpx.Request) -> httpx.Response:
        hits.append(req.url.path)
        if req.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": ISSUER, "userinfo_endpoint": ISSUER + "/userinfo"})
        await gate.wait()  # hold the answer until every request is waiting for it
        return userinfo(req)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    a = Authenticator(AuthSettings(issuer=ISSUER, verify="userinfo"), http=http)
    tasks = [asyncio.create_task(a.verify("opaque-alice")) for _ in range(5)]
    await asyncio.sleep(0.05)
    gate.set()
    assert [c["sub"] for c in await asyncio.gather(*tasks)] == ["auth0|alice"] * 5
    assert hits.count("/userinfo") == 1

    # The same for a token the provider rejects: every waiter is refused.
    gate.clear()
    tasks = [asyncio.create_task(a.verify("opaque-forged")) for _ in range(3)]
    await asyncio.sleep(0.05)
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, AuthError) and r.status == 401 for r in results)
    assert hits.count("/userinfo") == 2

    # allowed_users applies to userinfo answers too.
    b = Authenticator(AuthSettings(issuer=ISSUER, verify="userinfo", allowed_users=("@partner.test",)),
                      http=userinfo_provider()[1])
    with pytest.raises(AuthError) as e:
        await b.verify("opaque-alice")
    assert e.value.status == 403


def test_describe_token_never_logs_the_token():
    a = authenticator()
    tok = IDP.mint("alice@example.test")
    d = a.describe_token(tok)
    assert "JWT alg=RS256 kid=k1" in d and f"iss={ISSUER}" in d and tok not in d
    assert "signature" not in d and tok.split(".")[2] not in d
    jwe_head = base64.urlsafe_b64encode(json.dumps({"alg": "dir", "enc": "A256GCM", "iss": ISSUER}).encode()).decode().rstrip("=")
    d = a.describe_token(f"{jwe_head}..iv.secret-ciphertext.tag")
    assert "JWE" in d and "alg=dir" in d and "secret-ciphertext" not in d
    assert a.describe_token("s3cr3t-opaque") == "opaque token, 13 chars"
    assert a.describe_token(None) == f"no {HEADER} header"


# --------------------------------------------------------------- the app
def test_startup_requires_an_identity_provider(database_url):
    with pytest.raises(SystemExit):
        create_app(make_cfg(database_url), ops=FakeOps())                       # on Render, no AUTH_ISSUER
    with pytest.raises(SystemExit):
        create_app(make_cfg(database_url, dev=True), ops=FakeOps())             # dev can't disable it on Render
    with pytest.raises(SystemExit):
        create_app(make_cfg(database_url, on_render=False), ops=FakeOps())      # nor elsewhere without dev
    create_app(make_cfg(database_url, on_render=False, dev=True), ops=FakeOps())  # local dev only
    create_app(make_cfg(database_url, auth=AuthSettings(issuer=ISSUER)), ops=FakeOps())


async def test_every_request_needs_a_valid_token(database_url):
    async with running(make_cfg(database_url), FakeOps()) as (app, c):
        anon = httpx.AsyncClient(transport=c._transport, base_url="http://testserver", headers={"X-PGM-Request": "1"})
        async with anon:
            for path in ("/", "/app.js", "/api/system", "/api/migrations", "/api/me", "/api/global/jobs"):
                r = await anon.get(path)
                assert r.status_code == 401, path
            assert (await anon.post("/api/migrations", json={})).status_code == 401
            assert (await anon.get("/healthz")).status_code == 200
            forged = TestIdP().mint("alice@example.test")  # right kid, wrong key
            assert (await anon.get("/api/me", headers={HEADER: forged})).status_code == 401
            expired = IDP.mint(exp_in=-600)
            assert (await anon.get("/api/me", headers={HEADER: expired})).status_code == 401
        me = (await c.get("/api/me")).json()
        assert me["user"] == "alice@example.test" and me["authenticated"] and me["issuer"] == ISSUER


async def test_refusals_are_logged_with_the_token_shape_but_not_the_token(database_url, caplog):
    caplog.set_level(logging.INFO)
    async with running(make_cfg(database_url), FakeOps()) as (app, c):
        assert "authentication: issuer=" + ISSUER in caplog.text and "verify=jwt" in caplog.text
        r = await c.get("/api/me", headers={HEADER: "s3cr3t-opaque-value", "X-Forwarded-Email": "a@b.test"})
        assert r.status_code == 401 and r.json()["error"] == "access token is not a JWT"
        line = next(rec.getMessage() for rec in caplog.records if rec.getMessage().startswith("refused GET /api/me"))
        assert "opaque token, 19 chars" in line and "x-forwarded-access-token, x-forwarded-email" in line
        assert "AUTH_VERIFY=userinfo" in line and "s3cr3t" not in caplog.text and "a@b.test" not in caplog.text
        assert (await c.get("/api/me")).status_code == 200
        assert "signed in: alice@example.test (token checked by jwt)" in caplog.text


async def test_app_with_userinfo_checking(database_url):
    _, http = userinfo_provider()
    auth = Authenticator(AuthSettings(issuer=ISSUER, verify="userinfo", user_claim="email"), http=http)
    app = create_app(make_cfg(database_url), ops=FakeOps(), authenticator=auth)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
            me = (await c.get("/api/me", headers={HEADER: "opaque-alice"})).json()
            assert me == {"user": "alice@example.test", "name": "Alice", "issuer": ISSUER, "authenticated": True}
            assert (await c.get("/api/me", headers={HEADER: "opaque-forged"})).status_code == 401
            assert (await c.get("/api/me", headers={HEADER: "opaque-unverified"})).status_code == 403
            assert (await c.get("/api/me")).status_code == 401


async def test_jobs_and_migrations_record_who(database_url):
    async with running(make_cfg(database_url), FakeOps()) as (app, c):
        r = await c.post("/api/migrations", headers=auth_headers("carol@example.test"),
                         json={"id": "acme", "heroku_url": "postgres://u:p@h.example/d",
                               "render_url": "postgresql://r:q@dpg-x.render.com/e"})
        assert r.status_code == 201 and r.json()["created_by"] == "carol@example.test"
        r = await c.post("/api/migrations/acme/actions/preflight", headers=auth_headers("dave@example.test"), json={})
        job = r.json()
        assert job["started_by"] == "dave@example.test"
        v = (await c.get("/api/migrations/acme")).json()
        assert v["jobs"][0]["started_by"] == "dave@example.test"
