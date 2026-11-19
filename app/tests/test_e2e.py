"""End-to-end: the web app drives real containers through DockerOps.

  * Image & settings > Build: a "default image" sandbox (plain Debian, root)
    builds image/Dockerfile with BuildKit and pushes it to a local registry;
    import pulls that exact digest.
  * Two migrations, each in its own sandbox running the image just built:
    alpha goes through the whole runbook against the harness databases,
    including adding replication keys to the tables without one and
    removing them after cutover; beta targets the database without the
    replica-role grant and must be blocked by preflight.
  * The web app is restarted in the middle of the build to prove jobs resume.

Run: PGM_E2E=1 python -m pytest tests/test_e2e.py   (needs Docker; ~5 minutes)
Screenshots: PGM_SHOTS=<dir> (needs node + playwright).
"""

import asyncio
import os
import socket
import subprocess
import tempfile
from pathlib import Path

import httpx
import pytest
import uvicorn

from pgmigrate.config import DEFAULT_BASE_IMAGE, Config
from pgmigrate.web import create_app

from .conftest import wait_for
from .docker_ops import DockerOps
from .tokens import HEADER, auth_headers, authenticator

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(os.environ.get("PGM_E2E") != "1", reason="set PGM_E2E=1 (needs Docker)")]

REPO = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-f", str(REPO / "test" / "docker-compose.yml")]
NETWORK = "pg-migrate-test_default"
HEROKU = "postgres://app:app%3Apass@source:5432/app"
RENDER = "postgresql://migrator:migrator-pass@target:5432/app"
RENDER_NOGRANT = "postgresql://migrator:migrator-pass@target-nogrant:5432/app"
SHOTS = os.environ.get("PGM_SHOTS")


def sh(*args, check=True, **kw):
    return subprocess.run(args, check=check, capture_output=True, text=True, **kw)


def src_sql(sql: str) -> str:
    return sh(*COMPOSE, "exec", "-T", "-e", "PGPASSWORD=app:pass", "source", "psql", "-X", "-q", "-At",
              "-v", "ON_ERROR_STOP=1", "-h", "127.0.0.1", "-U", "app", "-d", "app", "-c", sql).stdout.strip()


def dst_sql(sql: str) -> str:
    return sh(*COMPOSE, "exec", "-T", "-e", "PGPASSWORD=migrator-pass", "target", "psql", "-X", "-q", "-At",
              "-v", "ON_ERROR_STOP=1", "-h", "127.0.0.1", "-U", "migrator", "-d", "app", "-c", sql).stdout.strip()


@pytest.fixture(scope="module")
def harness():
    env = os.environ
    sh(*COMPOSE, "down", "-v", "--remove-orphans", check=False)
    sh(*COMPOSE, "up", "-d", "--wait", "--quiet-pull", "source", "target", "target-nogrant")
    sh("docker", "rm", "-f", "pgm-registry", check=False)
    sh("docker", "run", "-d", "--name", "pgm-registry", "--network", NETWORK, "-p", "127.0.0.1:5055:5000",
       env.get("REGISTRY_IMAGE", "registry:2"))
    bk = tempfile.mkdtemp(prefix="pgm-bk-")
    cid = sh("docker", "create", env.get("BUILDKIT_IMAGE", "moby/buildkit:v0.35.0")).stdout.strip()
    for b in ("buildkitd", "buildctl", "buildkit-runc"):
        sh("docker", "cp", "-q", f"{cid}:/usr/bin/{b}", f"{bk}/{b}")
    sh("docker", "rm", cid)
    # The plain Debian "default image" has no CA certificates; Render's does.
    ca = env.get("CA_BUNDLE") or "/etc/ssl/certs/ca-certificates.crt"
    mounts = [f"{bk}:/opt/bk:ro", f"{ca}:/etc/ssl/certs/ca-certificates.crt:ro"]
    try:
        yield {"mounts": mounts}
    finally:
        for c in sh("docker", "ps", "-aq", "--filter", "label=pgm-fake=1", check=False).stdout.split():
            sh("docker", "rm", "-f", c, check=False)
        sh("docker", "rm", "-f", "pgm-registry", check=False)
        if env.get("KEEP") != "1":
            sh(*COMPOSE, "down", "-v", "--remove-orphans", check=False)


class Server:
    def __init__(self, cfg, ops):
        self.cfg, self.ops = cfg, ops
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"

    async def __aenter__(self):
        app = create_app(self.cfg, ops=self.ops, authenticator=authenticator())
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.task = asyncio.create_task(self.server.serve())
        await wait_for("server", lambda: _true(self.server.started), 20)
        self.client = httpx.AsyncClient(base_url=self.url, headers={"X-PGM-Request": "1", **auth_headers()}, timeout=60)
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()
        self.server.should_exit = True
        await self.task


async def _true(v):
    return v


async def shot(server, name, hash_, selector=None, dark=False):
    """Screenshot via Playwright (node). Async: the app runs on this event loop."""
    if not SHOTS:
        return
    os.makedirs(SHOTS, exist_ok=True)
    # The browser sends a token on every request, as Render's authentication would.
    env = {**os.environ, "PGM_AUTH_HEADER": HEADER, "PGM_TOKEN": auth_headers()[HEADER]}
    proc = await asyncio.create_subprocess_exec(
        "node", str(Path(__file__).with_name("screenshot.js")), f"{server.url}/{hash_}", f"{SHOTS}/{name}.png",
        selector or "", "dark" if dark else "", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    _, err = await asyncio.wait_for(proc.communicate(), 90)
    assert proc.returncode == 0, f"screenshot {name} failed:\n{err.decode()[-2000:]}"


async def job_done(c, mid, action, timeout=600, after=None):
    async def check():
        r = await c.get(f"/api/migrations/{mid}" if mid else "/api/global/jobs")
        jobs = [j for j in r.json()["jobs"] if j["action"] == action and (after is None or j["id"] > after)]
        return jobs[0] if jobs and jobs[0]["state"] not in ("queued", "running") else None
    return await wait_for(f"{mid or 'global'} {action}", check, timeout, interval=1)


async def run(c, mid, action, confirm=False, expect="succeeded", timeout=600, body=None):
    r = await c.post(f"/api/migrations/{mid}/actions/{action}",
                     json={**({"confirm": mid} if confirm else {}), **(body or {})})
    assert r.status_code == 202, r.text
    job = await job_done(c, mid, action, timeout, after=None)
    while job["id"] != r.json()["id"]:
        job = await job_done(c, mid, action, timeout)
    log = (await c.get(f"/api/migrations/{mid}/jobs/{job['id']}/log")).json()["text"]
    assert job["state"] == expect, f"{mid} {action}: {job['state']}\n{log[-3000:]}"
    return log


async def phase(c, mid, want, timeout=300):
    async def check():
        await c.post(f"/api/migrations/{mid}/refresh", json={})
        v = (await c.get(f"/api/migrations/{mid}")).json()
        st = (((v.get("status") or {}).get("report") or {}).get("status") or {})
        return st.get("phase") == want
    await wait_for(f"{mid} phase {want}", check, timeout, interval=2)


async def test_full_flow(harness, database_url):
    git_sha = sh("git", "-C", str(REPO), "rev-parse", "HEAD").stdout.strip()
    ops = DockerOps(network=NETWORK, default_image=os.environ.get("DEBIAN_IMAGE", "debian:trixie-slim"),
                    default_mounts=harness["mounts"], registry_alias={"pgm-registry:5000": "127.0.0.1:5055"})
    cfg = Config(database_url=database_url, on_render=True, secret_key="e2e-secret", render_api_key="x",
                 render_workspace_id="tea-x", github_registry="pgm-registry:5000/acme", github_username="bot",
                 github_token="ghp_e2e_token_never_in_sandbox", git_sha=git_sha,
                 base_image=os.environ.get("BASE_IMAGE", DEFAULT_BASE_IMAGE), buildkit_bin_dir="/opt/bk",
                 registry_insecure=True, job_poll_seconds=0.5, poll_seconds=3)
    try:
        # ------------------------------------------- build, with a restart mid-way
        async with Server(cfg, ops) as s:
            r = await s.client.post("/api/global/actions/build_image", json={})
            assert r.status_code == 202, r.text
            build_id = r.json()["id"]

            async def build_started():
                log = (await s.client.get(f"/api/global/jobs/{build_id}/log")).json()["text"]
                return "building pgm-registry:5000/acme/pg-migrate" in log
            await wait_for("build started", build_started, 120, interval=1)
        async with Server(cfg, ops) as s:  # a deploy restarted the web app
            c = s.client
            job = await job_done(c, None, "build_image", timeout=1200)
            log = (await c.get(f"/api/global/jobs/{build_id}/log")).json()["text"]
            assert job["state"] == "succeeded", log[-3000:]
            assert "resuming after a restart" in log and "[build] user=root" in log and "DIGEST=sha256:" in log
            assert "ghp_e2e_token" not in log
            sysinfo = (await c.get("/api/system")).json()
            assert sysinfo["latest_image"]["state"] == "ready"
            assert sysinfo["credential"]["paths"] == ["/token"] and sysinfo["credential"]["injection"] == "basic"
            await shot(s, "01-image", "#/image", "#img-list table")

            # --------------------------------------------- two migrations
            for mid, title, target in (("alpha", "Alpha production", RENDER), ("beta", "Beta staging", RENDER_NOGRANT)):
                r = await c.post("/api/migrations", json={"id": mid, "title": title, "heroku_url": HEROKU,
                                                          "render_url": target, "schemas": "public,billing"})
                assert r.status_code == 201, r.text

            await run(c, "beta", "create_sandbox")
            await run(c, "beta", "preflight", expect="failed")
            v = (await c.get("/api/migrations/beta")).json()
            replica = [b for b in v["preflight"]["blockers"] if "session_replication_role" in b]
            assert replica and "`CREATE USER migration_super_user WITH LOGIN PASSWORD" in replica[0]
            assert "`ALTER USER migration_super_user WITH SUPERUSER;`" in replica[0]

            await run(c, "alpha", "create_sandbox")
            await run(c, "alpha", "preflight", expect="failed")
            plan = (await c.get("/api/migrations/alpha")).json()["keys_plan"]
            assert [t["table"] for t in plan["tables"]] == ["public.audit_log", "public.settings"], plan
            assert "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS audit_log_mig_id_key" in plan["add_sql"]
            await shot(s, "02-alpha-blocked", "#/m/alpha", "#d-preflight .notice.bad")
            await shot(s, "02b-alpha-keys", "#/m/alpha", "[data-copy='add_keys']")
            # Run it for me: exactly the SQL the runbook showed, each statement
            # echoed with its result. Preflight then runs again by itself.
            keys = await run(c, "alpha", "add_keys", confirm=True, body={"expect": plan["add_sha"]})
            assert "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS audit_log_mig_id_key ON public.audit_log (mig_id);" in keys
            assert "public.audit_log: 500 row(s) filled" in keys and "=== Heroku: done" in keys, keys[-3000:]
            assert src_sql("SELECT count(*) - count(mig_id) FROM public.audit_log") == "0"

            async def passed():
                return ((await c.get("/api/migrations/alpha")).json().get("preflight") or {}).get("passed")
            await wait_for("preflight to pass after adding keys", passed, 120, interval=1)

            await run(c, "alpha", "schema")
            await run(c, "alpha", "start_sync")
            await phase(c, "alpha", "copied")
            await run(c, "alpha", "build_indexes")
            await phase(c, "alpha", "replicating")
            diag = await run(c, "alpha", "diagnose")
            assert "Bucardo running:     yes" in diag and "watchdog running:    yes" in diag, diag
            await shot(s, "03-alpha-replicating", "#/m/alpha", "#d-tables table")

            src_sql("INSERT INTO public.customers (email, external_ref, name) VALUES ('late@x.test', 'cus_late', 'Late')")
            src_sql("UPDATE public.orders SET status = 'paid' WHERE id < 50")
            await asyncio.sleep(3)
            cut = await run(c, "alpha", "cutover", confirm=True)
            assert "MISMATCH" not in cut and "cutover complete" in cut
            await phase(c, "alpha", "cutover-done")
            await run(c, "alpha", "cleanup_source", confirm=True)
            assert src_sql("SELECT count(*) FROM pg_namespace WHERE nspname = 'bucardo'") == "0"
            await shot(s, "03b-alpha-remove-keys", "#/m/alpha", "[data-copy='drop_keys']")
            plan = (await c.get("/api/migrations/alpha")).json()["keys_plan"]
            gone = await run(c, "alpha", "drop_keys", confirm=True, body={"expect": plan["drop_sha"]})
            assert "=== Heroku: done" in gone and "=== Render: done" in gone, gone[-3000:]
            q = "SELECT count(*) FROM pg_attribute WHERE attname = 'mig_id' AND attrelid IN ('public.audit_log'::regclass, 'public.settings'::regclass) AND NOT attisdropped"
            assert src_sql(q) == "0" and dst_sql(q) == "0"
            await run(c, "alpha", "teardown", confirm=True)
            v = (await c.get("/api/migrations/alpha")).json()
            assert v["torn_down"] and v["artifacts"], v
            art = await c.get(f"/api/migrations/alpha/artifacts/{v['artifacts'][0]['id']}")
            assert art.status_code == 200 and len(art.content) > 1000
            await shot(s, "04-alpha-finished", "#/m/alpha", "#d-artifacts a")
            await shot(s, "05-list", "#/", "tr[data-id='beta']")
            await shot(s, "06-list-dark", "#/", "tr[data-id='beta']", dark=True)

            # Data on Render matches Heroku.
            for t in ("public.customers", "public.orders", "public.order_items", "billing.invoices", "public.events",
                      "public.audit_log", "public.customer_tags", "public.settings"):
                q = f"SELECT count(*) || ':' || md5(string_agg(x::text, '|' ORDER BY x::text)) FROM {t} x"
                assert src_sql(q) == dst_sql(q), t

            # No secret reached the browser.
            everything = (await c.get("/api/migrations")).text + (await c.get("/api/system")).text
            for secret in ("migrator-pass", "app:pass", "app%3Apass", "ghp_e2e_token"):
                assert secret not in everything
    finally:
        await ops.cleanup()
