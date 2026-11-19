"""Unit tests: the web app against an in-memory Render (FakeOps) and a real
Postgres for its own state."""

import contextlib
import dataclasses
import shlex

import httpx
import pytest

from pgmigrate.config import Config
from pgmigrate.render_ops import ExecResult
from pgmigrate.vault import SecretBox, redact, secret_strings, summarize
from pgmigrate.web import create_app, validate

from .conftest import wait_for
from .fake_ops import FakeOps, remote_script_handler
from .tokens import auth_headers, authenticator

HEROKU = "postgres://u9abc:p$ss'w%3Ard@ec2-1-2-3-4.compute-1.amazonaws.com:5432/d1abc"
RENDER = "postgresql://render_user:s3cret-render@dpg-abc123-a.oregon-postgres.render.com/app_db"
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


# ------------------------------------------------------------------ http
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
        assert sb["image"] == "sbb-ready" and sb["timeout"] == 0 and sb["idle"] == 0 and sb["sandbox_group_id"] == "sbg-pg-migration"
        # The allow-list (this migration's two databases, plus Diagnostics' IP
        # service) is computed and recorded, but not applied: Render's
        # allow-lists admit HTTPS only, which would block Postgres.
        hosts = ["ec2-1-2-3-4.compute-1.amazonaws.com", "dpg-abc123-a.oregon-postgres.render.com", "api.ipify.org"]
        assert sb["network_policy"] == "allow-all"
        info = (await c.get("/api/migrations/acme")).json()["sandbox_info"]
        assert info["NETWORK_ALLOW_LIST"] == hosts and info["NETWORK_POLICY"] == "allow-all"
        assert info["NETWORK_ALLOW_LIST_ENFORCED"] is False
        assert sb["env"]["HEROKU_DATABASE_URL"] == HEROKU and sb["env"]["SCHEMAS"] == "public,billing"
        uploads = [call for call in ops.calls if call[0] == "copy_to"]
        assert uploads and uploads[0][2] == "/etc/pg-migrate/migration.env" and RENDER in uploads[0][3]


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
