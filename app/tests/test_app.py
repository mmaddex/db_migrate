"""Unit tests: the web app against an in-memory Render (FakeOps) and a real
Postgres for its own state."""

import asyncio
import contextlib
import dataclasses
import json
import shlex

import httpx
import pytest

from pgmigrate.config import Config
from pgmigrate.render_ops import ExecResult
from pgmigrate.vault import SecretBox, redact, secret_strings, summarize
from pgmigrate.web import create_app, superuser_url_error, validate

from .conftest import wait_for
from .fake_ops import FakeOps, remote_script_handler
from .tokens import auth_headers, authenticator

HEROKU = "postgres://u9abc:p$ss'w%3Ard@ec2-1-2-3-4.compute-1.amazonaws.com:5432/d1abc"
RENDER = "postgresql://render_user:s3cret-render@dpg-abc123-a.oregon-postgres.render.com/app_db"
SUPER = "postgresql://migration_super_user:sup3r-pass@dpg-abc123-a.oregon-postgres.render.com:5432/app_db"
TOKEN = "ghp_SECRETTOKEN1234567890"
HDR = {"X-PGM-Request": "1"}


def make_cfg(database_url, **kw) -> Config:
    base = dict(database_url=database_url, on_render=True, secret_key="test-secret",
                render_api_key="rnd_x", render_workspace_id="tea-x", github_registry="ghcr.io/acme",
                github_username="bot", github_token=TOKEN, git_sha="0123456789abcdef",
                job_poll_seconds=0.05, poll_seconds=0.3)
    base.update(kw)
    return Config(**base)


@contextlib.asynccontextmanager
async def running(cfg, ops):
    """The app with the test identity provider; the client sends a valid token."""
    app = create_app(cfg, ops=ops, authenticator=authenticator())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     headers={**HDR, **auth_headers()}) as c:
            yield app, c


async def wait_job(c, mid, action, timeout=10):
    async def done():
        r = await c.get(f"/api/migrations/{mid}" if mid else "/api/global/jobs")
        jobs = r.json()["jobs"]
        j = next((j for j in jobs if j["action"] == action), None)
        return j if j and j["state"] not in ("queued", "running") else None
    return await wait_for(f"{action} to finish", done, timeout)


async def create(c, mid="acme", **kw):
    r = await c.post("/api/migrations", json={"id": mid, "heroku_url": HEROKU, "render_url": RENDER, **kw})
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ pure
def test_vault_roundtrip_and_wrong_key():
    box = SecretBox("k1")
    blob = box.seal({"heroku_url": HEROKU})
    assert b"p$ss" not in blob
    assert box.open(blob) == {"heroku_url": HEROKU}
    with pytest.raises(ValueError):
        SecretBox("k2").open(blob)


def test_redaction_covers_encoded_and_decoded_passwords():
    s = secret_strings(HEROKU, RENDER)
    out = redact(f"a p$ss'w%3Ard b p$ss'w:rd c s3cret-render d {RENDER}", s)
    for leak in ("p$ss", "s3cret-render", "w:rd"):
        assert leak not in out
    assert summarize(RENDER)["display"] == "postgresql://render_user@dpg-abc123-a.oregon-postgres.render.com/app_db"


def test_validation():
    assert validate({"heroku_url": HEROKU, "render_url": RENDER}, require_urls=True) == {}
    bad = {
        "heroku_url": {"heroku_url": "mysql://a:b@h/d", "render_url": RENDER},
        "render_url": {"heroku_url": HEROKU, "render_url": "postgresql://u:p@dpg-abc123-a/app_db"},
        "sync_name": {"heroku_url": HEROKU, "render_url": RENDER, "sync_name": "1 bad"},
        "schemas": {"heroku_url": HEROKU, "render_url": RENDER, "schemas": "public;drop"},
        "plan": {"heroku_url": HEROKU, "render_url": RENDER, "plan": "huge"},
    }
    for field, body in bad.items():
        assert field in validate(body, require_urls=True), field
    assert "heroku_url" in validate({"heroku_url": "postgres://u@h/d", "render_url": RENDER}, require_urls=True)
    assert validate({}, require_urls=False) == {}
    # Hosts go into the sandbox's network allow-list, which takes DNS names only.
    errs = validate({"heroku_url": "postgres://u:p@54.1.2.3:5432/d", "render_url": RENDER}, require_urls=True)
    assert "allow-list" in errs["heroku_url"]
    errs = validate({"heroku_url": HEROKU, "render_url": RENDER, "slack_webhook_url": "https://10.1.1.1/hook"},
                    require_urls=True)
    assert "allow-list" in errs["slack_webhook_url"]


def test_superuser_url_validation():
    assert validate({"heroku_url": HEROKU, "render_url": RENDER, "render_superuser_url": SUPER}, require_urls=True) == {}
    assert "render_superuser_url" in validate({"render_superuser_url": "postgresql://u@h/d"}, require_urls=False)
    assert superuser_url_error(RENDER, SUPER) == ""
    other_db = SUPER.replace("/app_db", "/other_db")
    assert "same host, port and database" in superuser_url_error(RENDER, other_db)
    other_host = SUPER.replace("dpg-abc123-a", "dpg-zzz999-a")
    assert "same host, port and database" in superuser_url_error(RENDER, other_host)
    same_user = RENDER.replace("s3cret-render", "another-pass")
    assert "different user" in superuser_url_error(RENDER, same_user)


# ------------------------------------------------------------------ http
async def test_superuser_url_is_kept_beside_the_render_url(database_url):
    async with running(make_cfg(database_url), FakeOps()) as (app, c):
        r = await c.post("/api/migrations", json={"id": "bad", "heroku_url": HEROKU, "render_url": RENDER,
                                                  "render_superuser_url": SUPER.replace("/app_db", "/x")})
        assert r.status_code == 400 and "render_superuser_url" in r.json()["fields"]
        v = await create(c, render_superuser_url=SUPER)
        # The Render URL stays the app's credential; the superuser shows beside it.
        assert v["target"]["display"].startswith("postgresql://render_user@")
        assert v["target"]["superuser"].startswith("postgresql://migration_super_user@")
        body = (await c.get("/api/migrations/acme")).text
        assert "sup3r-pass" not in body and "s3cret-render" not in body
        row = await app.state.store.get_migration("acme")
        assert SecretBox("test-secret").open(row["secrets"])["render_superuser_url"] == SUPER
        # Moving the Render URL to another database leaves the superuser URL behind: refused.
        r = await c.patch("/api/migrations/acme", json={"render_url": RENDER.replace("/app_db", "/other_db")})
        assert r.status_code == 400 and "render_superuser_url" in r.json()["fields"]
        # Added later, as preflight asks.
        await create(c, mid="later")
        r = await c.patch("/api/migrations/later", json={"render_superuser_url": SUPER})
        assert r.status_code == 200 and r.json()["target"]["superuser"].startswith("postgresql://migration_super_user@")


async def test_guard(database_url):
    async with running(make_cfg(database_url), FakeOps()) as (app, c):
        assert (await c.get("/healthz")).status_code == 200
        # Cross-site request forgery: a signed-in browser can be made to POST
        # from another site, so writes need the custom header and same origin.
        r = await c.post("/api/migrations", json={}, headers={"X-PGM-Request": ""})
        assert r.status_code == 403
        r = await c.post("/api/migrations", json={}, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        r = await c.get("/api/system")
        assert r.status_code == 200 and "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert "s3cret" not in r.text and TOKEN not in r.text


async def test_create_never_returns_secrets(database_url):
    async with running(make_cfg(database_url), FakeOps()) as (app, c):
        v = await create(c, title="Acme")
        assert v["source"]["host"] == "ec2-1-2-3-4.compute-1.amazonaws.com"
        assert v["sync_name"] == "migrate_acme" and v["schemas"] == "public" and v["plan"] == "2c-4g"
        assert (await c.post("/api/migrations", json={"id": "acme", "heroku_url": HEROKU, "render_url": RENDER})).status_code == 400
        for path in ("/api/migrations", "/api/migrations/acme"):
            body = (await c.get(path)).text
            assert "p$ss" not in body and "s3cret-render" not in body and "secrets" not in body
        row = await app.state.store.get_migration("acme")
        assert b"s3cret-render" not in bytes(row["secrets"])


async def test_remote_job_preflight_parsed_and_logged(database_url):
    ops = FakeOps()
    ops.exec_handler = remote_script_handler({"preflight.sh": (
        "BLOCKER: tables without a primary key (Bucardo cannot replicate them): public.audit_log\n"
        "WARN:    HEROKU_DATABASE_URL has a ':' in its password\n"
        "INFO:    every table has a primary key\n"
        f"leaked? {RENDER}\n"
        "RESULT: fail (1 blocker(s), 1 warning(s))\n", 1)})
    async with running(make_cfg(database_url), ops) as (app, c):
        await create(c)
        sb = await ops.create_sandbox(group_id="g", name="pg-migrate-acme", image="sbb-1", plan="2c-4g",
                                      env={}, timeout_seconds=0, idle_timeout_seconds=0)
        await app.state.store.update_migration("acme", sandbox_id=sb["id"])
        r = await c.post("/api/migrations/acme/actions/preflight", json={})
        assert r.status_code == 202
        job = await wait_job(c, "acme", "preflight")
        assert job["state"] == "failed" and job["exit_code"] == 1
        v = (await c.get("/api/migrations/acme")).json()
        assert v["preflight"]["passed"] is False
        assert len(v["preflight"]["blockers"]) == 1 and len(v["preflight"]["warnings"]) == 1
        log = (await c.get(f"/api/migrations/acme/jobs/{job['id']}/log")).json()["text"]
        assert "public.audit_log" in log and "leaked? ****" in log and "s3cret-render" not in log
        # The status poller ran status.sh in the sandbox.
        await wait_for("status", lambda: _status_seen(c), 5)


async def _status_seen(c):
    v = (await c.get("/api/migrations/acme")).json()
    return v.get("status") and v["status"].get("report") and v["status"]["report"].get("status")


async def test_busy_confirm_cancel(database_url):
    ops = FakeOps()
    inner = remote_script_handler({})

    def handler(sid, cmd):  # schema.sh never finishes
        if cmd.startswith("/opt/migrate/job.sh status schema.sh"):
            return ExecResult(0, "running\n", "")
        return inner(sid, cmd)
    ops.exec_handler = handler
    async with running(make_cfg(database_url), ops) as (app, c):
        await create(c)
        sb = await ops.create_sandbox(group_id="g", name="n", image="i", plan="2c-4g", env={}, timeout_seconds=0, idle_timeout_seconds=0)
        await app.state.store.update_migration("acme", sandbox_id=sb["id"])
        assert (await c.post("/api/migrations/acme/actions/cutover", json={"confirm": "nope"})).status_code == 400
        assert (await c.post("/api/migrations/acme/actions/build_image", json={})).status_code == 404
        job = (await c.post("/api/migrations/acme/actions/schema", json={})).json()
        r = await c.post("/api/migrations/acme/actions/start_sync", json={})
        assert r.status_code == 409 and "Load schema" in r.json()["error"]
        # Read-only steps may run alongside.
        assert (await c.post("/api/migrations/acme/actions/diagnose", json={})).status_code == 202
        await wait_for("schema running", lambda: _job_state(c, job["id"], "running"))
        assert (await c.post(f"/api/migrations/acme/jobs/{job['id']}/cancel", json={})).status_code == 202
        done = await wait_job(c, "acme", "schema")
        assert done["state"] == "cancelled"
        assert any(call[0] == "exec" and "job.sh stop schema.sh" in call[2] for call in ops.calls)
        # Settings lock while a sandbox exists; delete needs teardown.
        assert (await c.patch("/api/migrations/acme", json={"schemas": "public,app"})).status_code == 409
        assert (await c.patch("/api/migrations/acme", json={"title": "Acme prod"})).json()["title"] == "Acme prod"
        assert (await c.request("DELETE", "/api/migrations/acme", json={"confirm": "acme"})).status_code == 409


async def _job_state(c, jid, state):
    r = await c.get(f"/api/migrations/acme/jobs/{jid}/log")
    return r.json()["job"]["state"] == state


async def test_create_sandbox_needs_image_then_passes_env(database_url):
    ops = FakeOps()
    ops.exec_handler = lambda sid, cmd: ExecResult(0, "root\nx86_64\nPG_MAJOR=17\n" if cmd.startswith("id") else "", "")
    async with running(make_cfg(database_url), ops) as (app, c):
        await create(c, schemas="public, billing")
        await c.post("/api/migrations/acme/actions/create_sandbox", json={})
        job = await wait_job(c, "acme", "create_sandbox")
        assert job["state"] == "failed"
        await app.state.store.upsert_image("0123456789ab", ref="ghcr.io/acme/pg-migrate:0123456789ab",
                                           image_id="sbb-ready", state="ready")
        await c.post("/api/migrations/acme/actions/create_sandbox", json={})
        await wait_for("sandbox", lambda: _sandbox_set(c), 10)
        sb = next(iter(ops.sandboxes.values()))
        # No lifetime limit; suspends after 5 minutes without a command (between steps).
        assert sb["image"] == "sbb-ready" and sb["timeout"] == 0 and sb["idle"] == 300 and sb["sandbox_group_id"] == "sbg-pg-migration"
        # The allow-list (this migration's two databases, plus Diagnostics' IP
        # service) is computed and recorded, but not applied: Render's
        # allow-lists admit HTTPS only, which would block Postgres.
        hosts = ["ec2-1-2-3-4.compute-1.amazonaws.com", "dpg-abc123-a.oregon-postgres.render.com", "api.ipify.org"]
        assert sb["network_policy"] == "allow-all"
        info = (await c.get("/api/migrations/acme")).json()["sandbox_info"]
        assert info["NETWORK_ALLOW_LIST"] == hosts and info["NETWORK_POLICY"] == "allow-all"
        assert info["NETWORK_ALLOW_LIST_ENFORCED"] is False and info["IDLE_TIMEOUT_SECONDS"] == 300
        assert sb["env"]["HEROKU_DATABASE_URL"] == HEROKU and sb["env"]["SCHEMAS"] == "public,billing"
        uploads = [call for call in ops.calls if call[0] == "copy_to"]
        assert uploads and uploads[0][2] == "/etc/pg-migrate/migration.env" and RENDER in uploads[0][3]
        assert "RENDER_SUPERUSER_URL" not in sb["env"]


async def test_superuser_url_reaches_the_sandbox_and_is_redacted(database_url):
    ops = FakeOps()
    handler = remote_script_handler({"preflight.sh": (f"leaked? {SUPER} sup3r-pass\nRESULT: pass (0 blocker(s), 0 warning(s))\n", 0)})
    ops.exec_handler = lambda sid, cmd: (ExecResult(0, "root\nx86_64\nPG_MAJOR=17\n", "") if cmd.startswith("id")
                                         else handler(sid, cmd))
    async with running(make_cfg(database_url), ops) as (app, c):
        await create(c, render_superuser_url=SUPER)
        await app.state.store.upsert_image("0123456789ab", ref="ghcr.io/acme/pg-migrate:0123456789ab",
                                           image_id="sbb-ready", state="ready")
        await c.post("/api/migrations/acme/actions/create_sandbox", json={})
        await wait_for("sandbox", lambda: _sandbox_set(c), 10)
        sb = next(iter(ops.sandboxes.values()))
        assert sb["env"]["RENDER_DATABASE_URL"] == RENDER and sb["env"]["RENDER_SUPERUSER_URL"] == SUPER
        uploads = [call for call in ops.calls if call[0] == "copy_to"]
        assert "RENDER_SUPERUSER_URL=" in uploads[0][3]
        await c.post("/api/migrations/acme/actions/preflight", json={})
        job = await wait_job(c, "acme", "preflight")
        log = (await c.get(f"/api/migrations/acme/jobs/{job['id']}/log")).json()["text"]
        assert "leaked? **** ****" in log and "sup3r-pass" not in log


async def test_migration_allow_list_is_applied_once_enforced(database_url, monkeypatch):
    import pgmigrate.config as config_mod
    monkeypatch.setattr(config_mod, "ENFORCE_MIGRATION_ALLOW_LIST", True)
    ops = FakeOps()
    async with running(make_cfg(database_url), ops) as (app, c):
        await create(c)
        await app.state.store.upsert_image("0123456789ab", ref="ghcr.io/acme/pg-migrate:0123456789ab",
                                           image_id="sbb-ready", state="ready")
        await c.post("/api/migrations/acme/actions/create_sandbox", json={})
        await wait_for("sandbox", lambda: _sandbox_set(c), 10)
        sb = next(iter(ops.sandboxes.values()))
        assert sb["network_policy"] == \
            "ec2-1-2-3-4.compute-1.amazonaws.com,dpg-abc123-a.oregon-postgres.render.com,api.ipify.org"
        info = (await c.get("/api/migrations/acme")).json()["sandbox_info"]
        assert info["NETWORK_POLICY"] == "allow-list" and info["NETWORK_ALLOW_LIST_ENFORCED"] is True


async def _sandbox_set(c):
    v = (await c.get("/api/migrations/acme")).json()
    j = next((j for j in v["jobs"] if j["action"] == "create_sandbox" and j["state"] == "succeeded"), None)
    return v.get("sandbox_id") and j


def build_handler(log_text: str, rc: int = 0):
    def handle(sid, cmd):
        if cmd.startswith("cat /tmp/pgm-build.rc"):
            return ExecResult(0, f"{rc}\n", "")
        if "wc -c </tmp/pgm-build.log" in cmd:
            off = int(cmd.split("tail -c +")[1].split()[0]) - 1 if "tail -c +" in cmd else 0
            data = log_text.encode()
            return ExecResult(0, f"SIZE={len(data)}\n" + data[off:].decode(), "")
        return ExecResult(0, "", "")
    return handle


async def test_build_uses_brokered_credential_and_token_never_reaches_sandbox(database_url):
    ops = FakeOps()
    ops.exec_handler = build_handler("[build] user=root arch=x86_64\nDIGEST=sha256:" + "ab" * 32 + "\n")
    cfg = make_cfg(database_url, build_network_policy="github.com,*.githubusercontent.com")
    async with running(cfg, ops) as (app, c):
        assert (await c.post("/api/global/actions/build_image", json={})).status_code == 202
        job = await wait_job(c, None, "build_image")
        log = (await c.get(f"/api/global/jobs/{job['id']}/log")).json()["text"]
        assert job["state"] == "succeeded", log
        # Credential: Basic user:token on the registry's /token endpoint, in the build group.
        put = [x for x in ops.calls if x[0] == "put_credential"]
        assert put == [("put_credential", "sbg-pg-migration-build", "registry-token", "ghcr.io", ["/token"], "basic", False)]
        assert ops.credential_values[("sbg-pg-migration-build", "registry-token")] == f"bot:{TOKEN}"
        # Build sandbox: default image, build group, no env; terminated afterwards.
        build = [x for x in ops.calls if x[0] == "create_sandbox"][0]
        assert build[2] == "sbg-pg-migration-build" and build[4] is None and build[6] == ()
        assert ops.sandboxes[build[1]]["network_policy"] == "github.com,*.githubusercontent.com"  # BUILD_NETWORK_POLICY
        assert ("terminate", build[1]) in ops.calls
        assert {"image/Dockerfile", "builder/build_image.sh", "sandbox/env.sh", ".dockerignore"} <= set(ops.uploads[build[1]])
        # Every supported major's client tools go into the image.
        start = [x[2] for x in ops.calls if x[0] == "exec" and "build_image.sh" in x[2]]
        assert len(start) == 1
        outer = shlex.split(start[0])
        assert "PG_CLIENT_MAJORS=14 15 16 17 18" in shlex.split(outer[outer.index("-c") + 1])
        # Imported by digest into the migration group, with registry credentials.
        imp = [x for x in ops.calls if x[0] == "import_image"][0]
        assert imp[1] == "sbg-pg-migration" and imp[3] == "ghcr.io/acme/pg-migrate@sha256:" + "ab" * 32 and imp[5]
        img = (await c.get("/api/system")).json()["latest_image"]
        assert img["state"] == "ready" and img["tag"] == "0123456789ab"
        # The token never reached a sandbox: not in any command, env, upload or the log.
        for call in ops.calls:
            assert TOKEN not in repr(call), call
        assert TOKEN not in log
        # A second build with the same token doesn't rewrite the credential.
        await c.post("/api/global/actions/build_image", json={})
        await wait_for("second build", lambda: _n_jobs(c, "build_image", 2))
        assert len([x for x in ops.calls if x[0] == "put_credential"]) == 1
    # A rotated token updates it.
    async with running(dataclasses.replace(cfg, github_token=TOKEN + "x"), ops) as (app, c):
        await c.post("/api/global/actions/build_image", json={})
        await wait_for("third build", lambda: _n_jobs(c, "build_image", 3))
        assert [x[-1] for x in ops.calls if x[0] == "put_credential"] == [False, True]


async def _n_jobs(c, action, n):
    jobs = (await c.get("/api/global/jobs")).json()["jobs"]
    done = [j for j in jobs if j["action"] == action and j["state"] in ("succeeded", "failed")]
    return len(done) >= n


async def test_failed_build_marks_image_failed(database_url):
    ops = FakeOps()
    ops.exec_handler = build_handler("ERROR build or push failed\n", rc=1)
    async with running(make_cfg(database_url), ops) as (app, c):
        await c.post("/api/global/actions/build_image", json={})
        job = await wait_job(c, None, "build_image")
        assert job["state"] == "failed"
        assert (await app.state.store.get_image("0123456789ab"))["state"] == "failed"
        assert not [x for x in ops.calls if x[0] == "import_image"]


async def test_resume_after_restart_does_not_restart_remote_script(database_url):
    ops = FakeOps()
    handler = remote_script_handler({"schema.sh": ("schema: loaded 7 tables\n", 0)})
    ops.exec_handler = handler
    cfg = make_cfg(database_url)
    async with running(cfg, ops) as (app, c):
        await create(c)
        sb = await ops.create_sandbox(group_id="g", name="n", image="i", plan="2c-4g", env={}, timeout_seconds=0, idle_timeout_seconds=0)
        await app.state.store.update_migration("acme", sandbox_id=sb["id"])
    # Simulate a job that was mid-flight when the previous process stopped.
    handler(sb["id"], "/opt/migrate/job.sh start schema.sh")
    ops.calls.clear()
    from pgmigrate.store import Store
    store = Store(database_url)
    await store.open()
    job = await store.create_job(migration_id="acme", action="schema", label="Load schema", read_only=False,
                                 data={"started": True, "offset": 0})
    await store.set_job(job["id"], state="running")
    await store.close()
    async with running(cfg, ops) as (app, c):
        done = await wait_job(c, "acme", "schema")
        assert done["state"] == "succeeded"
        log = (await c.get(f"/api/migrations/acme/jobs/{done['id']}/log")).json()["text"]
        assert "resuming after a restart" in log and "loaded 7 tables" in log
        assert not any(x[0] == "exec" and "job.sh start" in x[2] for x in ops.calls)


async def test_teardown_requires_cleanup_and_saves_artifacts(database_url):
    ops = FakeOps()
    verify = {"rc": 1}

    def handler(sid, cmd):
        if "cleanup_source.sh --verify-only" in cmd:
            return ExecResult(verify["rc"], "not clean\n" if verify["rc"] else "clean\n", "")
        return ExecResult(0, "", "")
    ops.exec_handler = handler
    async with running(make_cfg(database_url), ops) as (app, c):
        await create(c)
        sb = await ops.create_sandbox(group_id="g", name="n", image="i", plan="2c-4g", env={}, timeout_seconds=0, idle_timeout_seconds=0)
        await app.state.store.update_migration("acme", sandbox_id=sb["id"])
        assert (await c.post("/api/migrations/acme/actions/teardown", json={})).status_code == 400
        await c.post("/api/migrations/acme/actions/teardown", json={"confirm": "acme"})
        assert (await wait_job(c, "acme", "teardown"))["state"] == "failed"
        assert ops.sandboxes[sb["id"]]["status"] == "running"
        verify["rc"] = 0
        await c.post("/api/migrations/acme/actions/teardown", json={"confirm": "acme"})
        await wait_for("teardown", lambda: _torn_down(c))
        assert ops.sandboxes[sb["id"]]["status"] == "terminated"
        v = (await c.get("/api/migrations/acme")).json()
        assert v["torn_down"] and len(v["artifacts"]) == 1
        r = await c.get(f"/api/migrations/acme/artifacts/{v['artifacts'][0]['id']}")
        assert r.status_code == 200 and r.headers["content-type"] == "application/gzip" and len(r.content) > 50
        assert (await c.request("DELETE", "/api/migrations/acme", json={"confirm": "acme"})).status_code == 204


async def _torn_down(c):
    return (await c.get("/api/migrations/acme")).json()["torn_down"]


async def test_forced_teardown_only_when_no_sync_was_ever_started(database_url):
    """A sandbox that never started a sync can be torn down without the Heroku
    cleanup check (which needs a working database connection): Bucardo puts
    nothing on Heroku before Start sync."""
    ops = FakeOps()
    ops.exec_handler = lambda sid, cmd: ExecResult(1, "", "cannot connect to Heroku") \
        if "cleanup_source.sh" in cmd else ExecResult(0, "", "")
    async with running(make_cfg(database_url), ops) as (app, c):
        store = app.state.store
        await create(c)
        sb = await ops.create_sandbox(group_id="g", name="n", image="i", plan="2c-4g", env={}, timeout_seconds=0,
                                      idle_timeout_seconds=0)
        await store.update_migration("acme", sandbox_id=sb["id"])
        assert (await c.get("/api/migrations/acme")).json()["sync_started"] is False

        # A sync was started once: no forced teardown, from the API or the runbook.
        job = await store.create_job(migration_id="acme", action="start_sync", label="Start sync", read_only=False)
        await store.finish_job(job["id"], "failed", 1)
        assert (await c.get("/api/migrations/acme")).json()["sync_started"] is True
        r = await c.post("/api/migrations/acme/actions/teardown", json={"confirm": "acme", "force": True})
        assert r.status_code == 409 and "Clean up Heroku" in r.json()["error"]
        await store.create_job(migration_id="acme", action="teardown", label="Tear down", read_only=False,
                               data={"force": True})
        failed = await wait_job(c, "acme", "teardown")
        log = (await c.get(f"/api/migrations/acme/jobs/{failed['id']}/log")).json()["text"]
        assert failed["state"] == "failed" and "Clean up Heroku" in log
        assert ops.sandboxes[sb["id"]]["status"] == "running"

        # Never synced: forced teardown skips the check and terminates.
        await store._exec("DELETE FROM jobs WHERE migration_id = 'acme' AND action = 'start_sync'")
        r = await c.post("/api/migrations/acme/actions/teardown", json={"confirm": "acme", "force": True})
        assert r.status_code == 202
        await wait_for("teardown", lambda: _torn_down(c))
        assert ops.sandboxes[sb["id"]]["status"] == "terminated"
        assert not any(x[0] == "exec" and "cleanup_source.sh" in x[2] for x in ops.calls)
        done = (await c.get("/api/migrations/acme")).json()["jobs"][0]
        log = (await c.get(f"/api/migrations/acme/jobs/{done['id']}/log")).json()["text"]
        assert "no sync was ever started" in log


async def test_new_sandbox_after_a_teardown_before_any_sync(database_url):
    """Preflight can ask for a settings change (the Render superuser URL), and
    settings are locked while a sandbox exists: tear down, edit, create again."""
    ops = FakeOps()
    ops.exec_handler = lambda sid, cmd: ExecResult(0, "root\nx86_64\nPG_MAJOR=17\n" if cmd.startswith("id") else "", "")
    async with running(make_cfg(database_url), ops) as (app, c):
        store = app.state.store
        await create(c)
        await store.upsert_image("0123456789ab", ref="ghcr.io/acme/pg-migrate:0123456789ab",
                                 image_id="sbb-ready", state="ready")
        await c.post("/api/migrations/acme/actions/create_sandbox", json={})
        await wait_for("sandbox", lambda: _sandbox_set(c), 10)
        first = (await c.get("/api/migrations/acme")).json()["sandbox_id"]
        await store.update_migration("acme", preflight={"at": "x", "passed": True, "blockers": [], "warnings": [], "info": []})
        assert (await c.patch("/api/migrations/acme", json={"render_superuser_url": SUPER})).status_code == 409

        r = await c.post("/api/migrations/acme/actions/teardown", json={"confirm": "acme", "force": True})
        assert r.status_code == 202
        await wait_for("teardown", lambda: _torn_down(c))
        listed = next(m for m in (await c.get("/api/migrations")).json() if m["id"] == "acme")
        assert listed["torn_down"] and listed["sync_started"] is False
        assert (await c.patch("/api/migrations/acme", json={"render_superuser_url": SUPER})).status_code == 200

        assert (await c.post("/api/migrations/acme/actions/create_sandbox", json={})).status_code == 202

        async def replaced():
            v = (await c.get("/api/migrations/acme")).json()
            return v if v["sandbox_id"] != first and not v["torn_down"] else None
        v = await wait_for("new sandbox", replaced, 10)
        assert ops.sandboxes[first]["status"] == "terminated"
        assert ops.sandboxes[v["sandbox_id"]]["env"]["RENDER_SUPERUSER_URL"] == SUPER
        # The pass was for the old settings; the new sandbox needs a new preflight.
        assert v["preflight"] is None


async def test_no_new_sandbox_for_a_finished_migration(database_url):
    async with running(make_cfg(database_url), FakeOps()) as (app, c):
        store = app.state.store
        await create(c)
        job = await store.create_job(migration_id="acme", action="start_sync", label="Start sync", read_only=False)
        await store.finish_job(job["id"], "succeeded", 0)
        await store._exec("UPDATE migrations SET sandbox_id = 'sb-old', torn_down_at = now() WHERE id = 'acme'")
        r = await c.post("/api/migrations/acme/actions/create_sandbox", json={})
        assert r.status_code == 409 and "finished" in r.json()["error"]


# ------------------------------------------------- suspending between steps
def _status_checks(ops):
    return sum(1 for x in ops.calls if x[0] == "exec" and x[2].startswith("/opt/migrate/status.sh"))


async def _report(c):
    v = (await c.get("/api/migrations/acme")).json()
    return (v.get("status") or {}).get("report") or {}


async def _sandbox_state(c):
    return ((await _report(c)).get("sandbox") or {}).get("status")


async def _new_sandbox(app, ops, idle=300):
    sb = await ops.create_sandbox(group_id="sbg-pg-migration", name="pg-migrate-acme", image="i", plan="2c-4g",
                                  env={}, timeout_seconds=0, idle_timeout_seconds=idle)
    await app.state.store.update_migration("acme", sandbox_id=sb["id"])
    return sb["id"]


async def test_a_step_wakes_the_suspended_sandbox_then_leaves_it_idle(database_url):
    """Render suspends the sandbox between steps. The next step wakes it;
    afterwards one status check reads the step's results, and the periodic
    checks don't run commands in it again, so it can suspend."""
    ops = FakeOps()
    ops.exec_handler = remote_script_handler({"preflight.sh": ("INFO: ok\nRESULT: pass\n", 0)})
    async with running(make_cfg(database_url), ops) as (app, c):
        await create(c)
        sid = await _new_sandbox(app, ops)
        ops.suspend(sid)
        await wait_for("the suspended state", lambda: _is(c, "suspended"))
        assert (await _report(c)).get("error") is None and _status_checks(ops) == 0

        await c.post("/api/migrations/acme/actions/preflight", json={})
        job = await wait_job(c, "acme", "preflight")
        assert job["state"] == "succeeded"
        start = next(i for i, x in enumerate(ops.calls) if x[0] == "exec" and "job.sh start" in x[2])
        assert ops.calls.index(("wake", sid)) < start
        log = (await c.get(f"/api/migrations/acme/jobs/{job['id']}/log")).json()["text"]
        assert f"waking sandbox {sid}" in log

        await wait_for("the check after the step", lambda: _at_least(ops, 1))
        checks = _status_checks(ops)
        await asyncio.sleep(1.2)  # four periodic checks
        assert _status_checks(ops) == checks
        r = await _report(c)
        assert r["sandbox"]["status"] == "running" and r["keep_awake"] is None

        ops.suspend(sid)  # the idle timeout passed
        await wait_for("the suspended state", lambda: _is(c, "suspended"))
        await asyncio.sleep(0.7)
        assert ops.sandboxes[sid]["status"] == "suspended" and ("resumed", sid) not in ops.calls[start:]


async def _is(c, state):
    return await _sandbox_state(c) == state


async def _at_least(ops, n):
    return _status_checks(ops) >= n


async def test_sandbox_stays_awake_while_bucardo_replicates(database_url):
    """From Start sync until a successful Cut over, Bucardo must keep running
    in the background, which doesn't count as activity: the status checks
    keep the sandbox awake and wake it if it suspended anyway."""
    ops = FakeOps()
    markers = {}

    def handler(sid, cmd):
        if cmd.startswith("/opt/migrate/status.sh"):
            return ExecResult(0, json.dumps({"phase": "replicating", "markers": markers, "tables": [], "totals": {}}), "")
        return ExecResult(0, "", "")
    ops.exec_handler = handler
    async with running(make_cfg(database_url), ops) as (app, c):
        store = app.state.store
        await create(c)
        sid = await _new_sandbox(app, ops)

        # Start sync ran, but no status check saw its markers yet: the job
        # history alone keeps the sandbox awake.
        markers["sync.started"] = "2026-01-01T00:00:00Z"
        job = await store.create_job(migration_id="acme", action="start_sync", label="Start sync", read_only=False)
        await store.finish_job(job["id"], "succeeded", 0)
        ops.suspend(sid)  # e.g. the web app was down for longer than the idle timeout
        await wait_for("the sandbox to be woken", lambda: _is(c, "running"))
        assert ("resumed", sid) in ops.calls
        assert (await _report(c))["keep_awake"] == "Bucardo is replicating until cutover"
        checks = _status_checks(ops)
        await asyncio.sleep(0.8)
        assert _status_checks(ops) > checks

        # A cutover that failed (row counts didn't match) leaves Bucardo running.
        job = await store.create_job(migration_id="acme", action="cutover", label="Cut over", read_only=False)
        await store.finish_job(job["id"], "failed", 1)
        checks = _status_checks(ops)
        await asyncio.sleep(0.8)
        assert _status_checks(ops) > checks

        # One that succeeded stopped Bucardo: the sandbox may suspend again.
        markers["cutover.done"] = "2026-01-01T01:00:00Z"
        job = await store.create_job(migration_id="acme", action="cutover", label="Cut over", read_only=False)
        await store.finish_job(job["id"], "succeeded", 0)
        await wait_for("cutover in the status", lambda: _marker(c, "cutover.done"))
        await asyncio.sleep(0.4)  # a check may still be running
        checks = _status_checks(ops)
        await asyncio.sleep(1.0)
        assert _status_checks(ops) == checks
        assert (await _report(c))["keep_awake"] is None
        resumed = ops.calls.count(("resumed", sid))
        ops.suspend(sid)
        await wait_for("the suspended state", lambda: _is(c, "suspended"))
        await asyncio.sleep(0.7)
        assert ops.calls.count(("resumed", sid)) == resumed


async def _marker(c, name):
    return name in (((await _report(c)).get("status") or {}).get("markers") or {})


async def test_teardown_wakes_a_suspended_sandbox(database_url):
    ops = FakeOps()
    ops.exec_handler = lambda sid, cmd: ExecResult(0, "clean\n", "") if "--verify-only" in cmd else ExecResult(0, "", "")
    async with running(make_cfg(database_url), ops) as (app, c):
        await create(c)
        sid = await _new_sandbox(app, ops)
        ops.suspend(sid)
        await c.post("/api/migrations/acme/actions/teardown", json={"confirm": "acme"})
        await wait_for("teardown", lambda: _torn_down(c))
        v = (await c.get("/api/migrations/acme")).json()
        assert ("wake", sid) in ops.calls and len(v["artifacts"]) == 1
        assert ops.sandboxes[sid]["status"] == "terminated"


async def test_create_sandbox_reuses_a_suspended_one(database_url):
    """Rerunning Create sandbox finds the migration's suspended sandbox and
    wakes it rather than starting a second one, and records the idle timeout
    that sandbox was created with."""
    ops = FakeOps()
    async with running(make_cfg(database_url), ops) as (app, c):
        await create(c)
        await app.state.store.upsert_image("0123456789ab", ref="ghcr.io/acme/pg-migrate:0123456789ab",
                                           image_id="sbb-ready", state="ready")
        sid = await _new_sandbox(app, ops, idle=0)  # made before suspending between steps
        await app.state.store.update_migration("acme", sandbox_id=None)
        ops.suspend(sid)
        await c.post("/api/migrations/acme/actions/create_sandbox", json={})
        assert (await wait_job(c, "acme", "create_sandbox"))["state"] == "succeeded"
        v = (await c.get("/api/migrations/acme")).json()
        assert list(ops.sandboxes) == [sid] and v["sandbox_id"] == sid and ("wake", sid) in ops.calls
        assert v["sandbox_info"]["IDLE_TIMEOUT_SECONDS"] == 0


async def test_a_probe_asked_for_during_a_check_is_not_lost():
    """After a step, the worker asks for a status check that runs status.sh.
    If a periodic check (which may not run it) is in flight, the request is
    queued rather than dropped, or the step's results wouldn't show until
    the next step."""
    from pgmigrate.worker import Worker

    engine = type("E", (), {})()
    w = Worker(make_cfg("postgresql://unused"), store=None, ops=None, engine=engine)
    seen, gate = [], asyncio.Event()

    async def once(mid, probe):
        seen.append(probe)
        await gate.wait()
    w._refresh_once = once
    w.request_refresh("acme", probe=False)
    await asyncio.sleep(0)
    w.request_refresh("acme", probe=True)
    w.request_refresh("acme", probe=False)
    gate.set()
    await wait_for("both checks", lambda: _done(w))
    assert seen == [False, True]


async def _done(w):
    return not w.refreshing


# ------------------------------------------------------- replication keys
ADD_SHA = "a" * 64
DROP_SHA = "d" * 64
ADD_SQL = "SET statement_timeout = 0;\n\n-- 1/1 public.audit_log: no primary key or unique index\nALTER TABLE public.audit_log ADD COLUMN IF NOT EXISTS mig_id bigint;\n"
DROP_SQL = "DROP INDEX CONCURRENTLY IF EXISTS public.audit_log_mig_id_key;\n"


def keys_handler(plans: list[dict], outputs: dict):
    """remote_script_handler, plus keys.sh's plan.json: each read returns the
    next plan in the list (the last one repeats)."""
    inner = remote_script_handler(outputs)
    reads = {"n": 0}

    def handle(sid, cmd):
        if cmd.startswith("cat /var/lib/migrate/artifacts/keys/plan.json"):
            plan = plans[min(reads["n"], len(plans) - 1)]
            reads["n"] += 1
            return ExecResult(0, json.dumps(plan), "")
        return inner(sid, cmd)
    return handle


async def test_keys_plan_from_preflight_and_run_only_the_sql_shown(database_url):
    """Preflight stores keys.sh's plan; Run it for me runs keys.sh add with the
    hash of the SQL the page showed, and refuses any other; afterwards the
    plan is read again and preflight runs by itself."""
    before = {"tables": [{"table": "public.audit_log", "reason": "no primary key or unique index", "manual": None,
                          "owner_ok": True, "col": "mig_id"}],
              "added": [], "add_sql": ADD_SQL, "add_sha": ADD_SHA, "drop_sql": "", "drop_sha": "",
              "report": [{"level": "BLOCKER", "text": "..."}]}
    after = {"tables": [], "added": [{"table": "public.audit_log", "col": "mig_id",
                                      "indexes": ["public.audit_log_mig_id_key"]}],
             "add_sql": "", "add_sha": "", "drop_sql": DROP_SQL, "drop_sha": DROP_SHA}
    ops = FakeOps()
    ops.exec_handler = keys_handler([before, after], {
        "preflight.sh": ("BLOCKER: tables without a primary key (Bucardo cannot replicate them): public.audit_log\n"
                         "RESULT: fail (1 blocker(s), 0 warning(s))\n", 1),
        "keys.sh": ("ALTER TABLE public.audit_log ADD COLUMN IF NOT EXISTS mig_id bigint;\nALTER TABLE\n", 0)})
    async with running(make_cfg(database_url), ops) as (app, c):
        await create(c)
        await _new_sandbox(app, ops, idle=0)
        # Nothing to run before a preflight has planned it.
        r = await c.post("/api/migrations/acme/actions/add_keys", json={"confirm": "acme", "expect": ADD_SHA})
        assert r.status_code == 409 and "preflight" in r.json()["error"]

        await c.post("/api/migrations/acme/actions/preflight", json={})
        await wait_job(c, "acme", "preflight")
        v = (await c.get("/api/migrations/acme")).json()
        assert v["keys_plan"]["add_sql"] == ADD_SQL and v["keys_plan"]["add_sha"] == ADD_SHA
        assert "report" not in v["keys_plan"]

        # It changes Heroku's tables: typed confirmation, and only the SQL shown.
        assert (await c.post("/api/migrations/acme/actions/add_keys", json={"expect": ADD_SHA})).status_code == 400
        r = await c.post("/api/migrations/acme/actions/add_keys", json={"confirm": "acme", "expect": "b" * 64})
        assert r.status_code == 409 and "changed" in r.json()["error"]
        r = await c.post("/api/migrations/acme/actions/add_keys", json={"confirm": "acme", "expect": ADD_SHA})
        assert r.status_code == 202
        job = await wait_job(c, "acme", "add_keys")
        assert job["state"] == "succeeded"
        starts = [x[2] for x in ops.calls if x[0] == "exec" and "job.sh start keys.sh" in x[2]]
        assert starts == [f"/opt/migrate/job.sh start keys.sh add --confirm --expect {ADD_SHA}"]
        log = (await c.get(f"/api/migrations/acme/jobs/{job['id']}/log")).json()["text"]
        assert "ADD COLUMN IF NOT EXISTS mig_id" in log and "queued Run preflight" in log

        # The plan as keys.sh left it, and a fresh preflight.
        v = (await c.get("/api/migrations/acme")).json()
        assert v["keys_plan"]["tables"] == [] and v["keys_plan"]["drop_sha"] == DROP_SHA
        await wait_for("a second preflight", lambda: _n_migration_jobs(c, "preflight", 2))

        # Removing them is confirmed the same way, with its own SQL's hash.
        r = await c.post("/api/migrations/acme/actions/drop_keys", json={"confirm": "acme", "expect": ADD_SHA})
        assert r.status_code == 409
        r = await c.post("/api/migrations/acme/actions/drop_keys", json={"confirm": "acme", "expect": DROP_SHA})
        assert r.status_code == 202
        await wait_job(c, "acme", "drop_keys")
        assert any(x[0] == "exec" and x[2] == f"/opt/migrate/job.sh start keys.sh drop --confirm --expect {DROP_SHA}"
                   for x in ops.calls)


async def _n_migration_jobs(c, action, n):
    jobs = (await c.get("/api/migrations/acme")).json()["jobs"]
    return len([j for j in jobs if j["action"] == action and j["state"] not in ("queued", "running")]) >= n


async def test_keys_plan_with_a_bad_hash_cant_be_run(database_url):
    """A plan whose hash isn't a sha256 is stored without one, so nothing can run it."""
    ops = FakeOps()
    plan = {"tables": [{"table": "public.t"}], "added": [], "add_sql": "SELECT 1;\n", "add_sha": "not-a-hash; rm -rf /",
            "drop_sql": "", "drop_sha": ""}
    ops.exec_handler = keys_handler([plan], {"preflight.sh": ("RESULT: fail\n", 1)})
    async with running(make_cfg(database_url), ops) as (app, c):
        await create(c)
        await _new_sandbox(app, ops, idle=0)
        await c.post("/api/migrations/acme/actions/preflight", json={})
        await wait_job(c, "acme", "preflight")
        assert (await c.get("/api/migrations/acme")).json()["keys_plan"]["add_sha"] == ""
        r = await c.post("/api/migrations/acme/actions/add_keys", json={"confirm": "acme", "expect": "not-a-hash; rm -rf /"})
        assert r.status_code == 409
