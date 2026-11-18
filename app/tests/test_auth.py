"""Strict per-request authentication: token verification, key discovery and
rotation, and the app refusing anything without a valid token."""

import base64
import hashlib
import hmac
import json

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
    ("not-a-jwt", "malformed"),
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
    assert s.token_header == "X-Forwarded-Access-Token" and s.user_claim == "sub"
    assert settings_from_env(lambda k: "") is None


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
            return httpx.Response(200, json={"name": "Alice Example"})
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


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
