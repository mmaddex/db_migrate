"""The runbook: what each button does, as resumable jobs.

Every action becomes a row in `jobs`. The worker runs it, appending output
to the job's log and checkpointing progress in the job's `data`, so a deploy
or restart of the web app resumes the job instead of losing it:

  * Sandbox scripts (preflight, schema, ...) run detached inside the
    migration's sandbox through /opt/migrate/job.sh. The app starts them,
    then tails their log by byte offset and polls their exit status.
  * Sandbox lifecycle steps (create, teardown, artifacts, snapshot) call the
    Render API and record IDs as they go.
  * The image build runs in its own sandbox from Render's default image,
    in a group whose brokered credential authenticates registry pushes.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shlex
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import Config
from .render_ops import OpsError, RenderOps
from .store import Store
from .vault import SecretBox, redact, secret_strings

log = logging.getLogger("pgmigrate.runbook")


@dataclass(frozen=True)
class Action:
    name: str
    label: str
    kind: str                      # remote | create_sandbox | teardown | fetch | snapshot | build | import
    script: str = ""
    args: tuple[str, ...] = ()
    destructive: bool = False      # needs the migration name typed to confirm
    read_only: bool = False        # may run alongside a state-changing job
    is_global: bool = False        # image jobs, not tied to a migration

    def public(self) -> dict:
        return {"name": self.name, "label": self.label, "destructive": self.destructive, "read_only": self.read_only}


ACTIONS: list[Action] = [
    Action("build_image", "Build & push image", "build", is_global=True),
    Action("import_image", "Import image", "import", is_global=True),

    Action("create_sandbox", "Create sandbox", "create_sandbox"),
    Action("preflight", "Run preflight", "remote", "preflight.sh", read_only=True),
    Action("schema", "Load schema", "remote", "schema.sh"),
    Action("start_sync", "Start sync", "remote", "start_sync.sh"),
    Action("build_indexes", "Build indexes", "remote", "build_indexes.sh"),
    Action("status_exact", "Exact row counts", "remote", "status.sh", ("--exact",), read_only=True),
    Action("diagnose", "Diagnostics", "remote", "diagnose.sh", read_only=True),
    Action("fetch_artifacts", "Fetch artifacts", "fetch", read_only=True),
    Action("snapshot", "Snapshot sandbox", "snapshot", read_only=True),
    Action("cutover", "Cut over", "remote", "cutover.sh", ("--confirm",), destructive=True),
    Action("cleanup_source", "Clean up Heroku", "remote", "cleanup_source.sh", ("--confirm",), destructive=True),
    Action("reset_target", "Reset Render target", "remote", "reset_target.sh", ("--confirm",), destructive=True),
    Action("teardown", "Tear down sandbox", "teardown", destructive=True),
]
BY_NAME = {a.name: a for a in ACTIONS}

PREFLIGHT_LINE = re.compile(r"^(BLOCKER|WARN|INFO):\s+(.*)$")
ARTIFACT_LIMIT = 200 * 1024 * 1024


class JobFailed(Exception):
    """Ends a job as failed with a message for the log."""

    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


class Cancelled(Exception):
    pass


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Ctx:
    """One running job."""

    job: dict
    migration: dict | None
    secrets: list[str] = field(default_factory=list)

    @property
    def data(self) -> dict:
        return self.job["data"]


class Engine:
    def __init__(self, cfg: Config, store: Store, ops: RenderOps, box: SecretBox | None):
        self.cfg, self.store, self.ops, self.box = cfg, store, ops, box
        self.on_migration_changed = None  # set by the worker: refresh status

    # ----------------------------------------------------------- plumbing
    async def say(self, ctx: Ctx, text: str) -> None:
        if not text.endswith("\n"):
            text += "\n"
        await self.store.append_log(ctx.job["id"], redact(text, ctx.secrets))

    async def note(self, ctx: Ctx, text: str) -> None:
        await self.say(ctx, f"[{ts()}] {text}")

    async def save(self, ctx: Ctx) -> None:
        await self.store.set_job(ctx.job["id"], data=ctx.data)

    async def check_cancel(self, ctx: Ctx) -> None:
        row = await self.store.get_job(ctx.job["id"])
        if row and row["data"].get("cancel"):
            raise Cancelled()

    def migration_secrets(self, migration: dict) -> dict:
        if not self.box:
            raise JobFailed("APP_SECRET_KEY is not set, so stored connection strings can't be read")
        return self.box.open(migration["secrets"])

    async def run(self, job: dict) -> None:
        """Run (or resume) one job to completion."""
        action = BY_NAME.get(job["action"])
        migration = await self.store.get_migration(job["migration_id"]) if job["migration_id"] else None
        ctx = Ctx(job=job, migration=migration)
        if migration is not None and self.box:
            try:
                s = self.migration_secrets(migration)
                ctx.secrets = secret_strings(s.get("heroku_url", ""), s.get("render_url", ""), s.get("slack_webhook_url", ""))
            except ValueError:
                pass
        if self.cfg.github_token:
            ctx.secrets += secret_strings(self.cfg.github_token, f"{self.cfg.github_username}:{self.cfg.github_token}")
        if job["state"] == "queued":
            await self.store.set_job(job["id"], state="running")
            await self.say(ctx, f"$ {action.label if action else job['action']}")
        else:
            await self.note(ctx, "resuming after a restart of the web app")
        code, state = 0, "succeeded"
        try:
            if action is None:
                raise JobFailed(f"unknown action {job['action']}")
            handler = getattr(self, f"do_{action.kind}")
            code = await handler(ctx, action) or 0
            state = "succeeded" if code == 0 else "failed"
        except Cancelled:
            await self.note(ctx, "cancelled")
            code, state = 130, "cancelled"
            await self.on_cancel(ctx, action)
        except JobFailed as e:
            await self.note(ctx, f"ERROR {e}")
            code, state = e.exit_code, "failed"
        except OpsError as e:
            await self.note(ctx, f"ERROR Render API: {e}")
            code, state = 1, "failed"
        except Exception as e:  # noqa: BLE001 - every failure must end the job
            log.exception("job %s failed", job["id"])
            await self.note(ctx, f"ERROR {type(e).__name__}: {redact(str(e), ctx.secrets)}")
            code, state = 1, "failed"
        await self.say(ctx, f"\n[{ts()}] exit {code} ({state})")
        # Record results before marking the job finished, so anyone who sees
        # the job end also sees what it changed.
        if migration is not None:
            await self.record_results(ctx, action, state)
        await self.store.finish_job(job["id"], state, code)
        if migration is not None and self.on_migration_changed:
            self.on_migration_changed(migration["id"])

    async def record_results(self, ctx: Ctx, action: Action | None, state: str) -> None:
        mid = ctx.migration["id"]
        if action and action.name == "preflight":
            await self.record_preflight(ctx, state)
        if action and action.name == "cleanup_source" and state == "succeeded":
            await self.store.update_migration(mid, cleanup_done_at=datetime.now(timezone.utc))

    async def on_cancel(self, ctx: Ctx, action: Action | None) -> None:
        if action and action.kind == "remote" and ctx.data.get("started") and ctx.migration:
            await self.ops.exec(ctx.migration["sandbox_id"], f"/opt/migrate/job.sh stop {action.script}", timeout=60)
        if action and action.kind == "build" and ctx.data.get("build_sandbox_id"):
            await self.ops.terminate(ctx.data["build_sandbox_id"])

    # ------------------------------------------------------ remote scripts
    async def require_running_sandbox(self, ctx: Ctx) -> str:
        m = ctx.migration
        if not m or not m.get("sandbox_id") or m.get("torn_down_at"):
            raise JobFailed("this migration has no sandbox; create one first")
        sb = await self.ops.get_sandbox(m["sandbox_id"])
        if not sb or sb["status"] != "running":
            raise JobFailed(f"sandbox {m['sandbox_id']} is {sb['status'] if sb else 'gone'}, not running")
        return m["sandbox_id"]

    async def do_remote(self, ctx: Ctx, action: Action) -> int:
        sid = await self.require_running_sandbox(ctx)
        script = action.script
        if not ctx.data.get("started"):
            cmd = "/opt/migrate/job.sh start " + shlex.join([script, *action.args])
            r = await self.ops.exec(sid, cmd, timeout=90)
            if not r.ok:
                await self.say(ctx, r.stdout + r.stderr)
                raise JobFailed(f"could not start {script} in the sandbox")
            ctx.data.update(started=True, offset=0)
            await self.save(ctx)
        while True:
            await self.pull_remote_log(ctx, sid, script)
            st = (await self.ops.exec(sid, f"/opt/migrate/job.sh status {script}", timeout=60)).stdout.strip()
            if st == "running":
                await self.check_cancel(ctx)
                await asyncio.sleep(self.cfg.job_poll_seconds)
                continue
            await self.pull_remote_log(ctx, sid, script)
            if st == "succeeded":
                return 0
            m = re.match(r"failed \(rc (\d+)\)", st)
            if m:
                return int(m.group(1))
            raise JobFailed(f"{script} ended without an exit code ({st or 'no status'})")

    async def pull_remote_log(self, ctx: Ctx, sid: str, script: str) -> None:
        off = int(ctx.data.get("offset", 0))
        r = await self.ops.exec(sid, f"/opt/migrate/job.sh log {script} {off}", timeout=90)
        head, _, body = r.stdout.partition("\n")
        if not head.startswith("SIZE="):
            return
        size = int(head[5:])
        if size < off:  # the remote log was replaced; start from the top
            ctx.data["offset"] = 0
            return
        if body:
            await self.store.append_log(ctx.job["id"], redact(body, ctx.secrets))
        if size != off:
            ctx.data["offset"] = size
            await self.save(ctx)

    async def record_preflight(self, ctx: Ctx, state: str) -> None:
        row = await self.store.get_job(ctx.job["id"])
        text = row["log"] if row else ""
        p = {"at": ts(), "blockers": [], "warnings": [], "info": [], "passed": False}
        saw_result = False
        for line in text.splitlines():
            m = PREFLIGHT_LINE.match(line)
            if m:
                key = {"BLOCKER": "blockers", "WARN": "warnings"}.get(m.group(1), "info")
                p[key].append(m.group(2))
            if line.startswith("RESULT: "):
                saw_result = True
        if saw_result:
            p["passed"] = state == "succeeded" and not p["blockers"]
            await self.store.update_migration(ctx.migration["id"], preflight=p)

    # ------------------------------------------------- sandbox lifecycle
    def sandbox_env(self, ctx: Ctx) -> dict[str, str]:
        m = ctx.migration
        s = self.migration_secrets(m)
        env = {"HEROKU_DATABASE_URL": s["heroku_url"], "RENDER_DATABASE_URL": s["render_url"],
               "SYNC_NAME": m["sync_name"], "SCHEMAS": m["schemas"], "SANDBOX_LABEL": m["id"]}
        if s.get("slack_webhook_url"):
            env["SLACK_WEBHOOK_URL"] = s["slack_webhook_url"]
        return env

    async def do_create_sandbox(self, ctx: Ctx, action: Action) -> int:
        m = ctx.migration
        if self.cfg.missing_for("render"):
            raise JobFailed("set " + ", ".join(self.cfg.missing_for("render")) + " on the web service")
        gid = await self.ops.ensure_group(self.cfg.sandbox_group)
        name = f"pg-migrate-{m['id']}"
        sid = ctx.data.get("sandbox_id")
        if not sid:
            image = await self.store.latest_ready_image()
            if not image:
                raise JobFailed("no imported image yet: build and import it on the Image page first")
            existing = await self.ops.find_sandbox(gid, name)
            if existing:
                sid = existing["id"]
                await self.note(ctx, f"reusing sandbox {sid} ({existing['status']}) named {name}")
            else:
                await self.note(ctx, f"creating sandbox {name} from image {image['tag']} ({image['image_id']}), plan {m['plan']}, no timeouts")
                sb = await self.ops.create_sandbox(group_id=gid, name=name, image=image["image_id"], plan=m["plan"],
                                                   env=self.sandbox_env(ctx), timeout_seconds=0, idle_timeout_seconds=0)
                sid = sb["id"]
            ctx.data.update(sandbox_id=sid, image_tag=image["tag"], image_id=image["image_id"])
            await self.save(ctx)
            await self.store.update_migration(
                m["id"], sandbox_id=sid, torn_down_at=None, cleanup_done_at=None, status=None, status_error=None,
                sandbox_info={"IMAGE_TAG": image["tag"], "IMAGE_ID": image["image_id"], "CREATED_AT": ts()})
        await self.wait_running(ctx, sid, timeout=900)
        if not ctx.data.get("env_file"):
            # Fallback for sandboxes whose exec sessions don't see the create-time
            # env (docs/spike.md): env.sh loads this file only if the variables
            # are missing.
            await self.upload_env_file(ctx, sid)
            ctx.data["env_file"] = True
            await self.save(ctx)
        r = await self.ops.exec(sid, "id -un; uname -m; cat /opt/migrate/image.env", timeout=60)
        await self.say(ctx, r.stdout)
        return 0

    async def upload_env_file(self, ctx: Ctx, sid: str) -> None:
        env = self.sandbox_env(ctx)
        fd, path = tempfile.mkstemp(prefix="pgm-env-")
        try:
            with os.fdopen(fd, "w") as f:
                for k, v in env.items():
                    f.write(f"{k}={v}\n")
            os.chmod(path, 0o600)
            await self.ops.exec(sid, "mkdir -p /etc/pg-migrate && chmod 700 /etc/pg-migrate", timeout=60)
            await self.ops.copy_to(sid, path, "/etc/pg-migrate/migration.env")
            await self.ops.exec(sid, "chmod 600 /etc/pg-migrate/migration.env", timeout=60)
        finally:
            os.unlink(path)

    async def wait_running(self, ctx: Ctx, sid: str, timeout: float) -> None:
        loop = asyncio.get_running_loop()
        deadline, last = loop.time() + timeout, None
        while True:
            sb = await self.ops.get_sandbox(sid)
            status = sb["status"] if sb else "gone"
            if status != last:
                await self.note(ctx, f"sandbox {sid}: {status}")
                last = status
            if status == "running":
                return
            if status in ("errored", "terminated", "gone"):
                raise JobFailed(f"sandbox {sid} is {status}")
            if loop.time() > deadline:
                raise JobFailed(f"sandbox {sid} still {status} after {timeout:.0f}s; rerun to keep waiting")
            await self.check_cancel(ctx)
            await asyncio.sleep(5)

    async def do_teardown(self, ctx: Ctx, action: Action) -> int:
        m = ctx.migration
        sid = m.get("sandbox_id")
        if not sid or m.get("torn_down_at"):
            raise JobFailed("no sandbox to tear down")
        sb = await self.ops.get_sandbox(sid)
        running = bool(sb and sb["status"] == "running")
        if ctx.data.get("force"):
            await self.note(ctx, "forced: not checking that Bucardo objects were removed from Heroku")
        elif m.get("cleanup_done_at"):
            await self.note(ctx, "cleanup of Heroku succeeded earlier")
        elif running:
            r = await self.ops.exec(sid, "/opt/migrate/cleanup_source.sh --verify-only", timeout=120)
            await self.say(ctx, r.stdout + r.stderr)
            if not r.ok:
                raise JobFailed("Bucardo objects remain on Heroku: run Clean up Heroku first")
        else:
            raise JobFailed("can't verify the cleanup: the sandbox isn't running")
        if running and not ctx.data.get("fetched"):
            try:
                await self.fetch(ctx, sid)
            except Exception as e:  # noqa: BLE001 - best effort before terminating
                await self.note(ctx, f"WARN final artifact fetch failed: {e}")
            ctx.data["fetched"] = True
            await self.save(ctx)
        if sb and sb["status"] not in ("terminated",):
            await self.ops.terminate(sid)
            await self.note(ctx, f"terminated sandbox {sid}")
        await self.store.update_migration(m["id"], torn_down_at=datetime.now(timezone.utc))
        return 0

    async def do_fetch(self, ctx: Ctx, action: Action) -> int:
        sid = await self.require_running_sandbox(ctx)
        await self.fetch(ctx, sid)
        return 0

    async def fetch(self, ctx: Ctx, sid: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        with tempfile.TemporaryDirectory(prefix="pgm-artifacts-") as tmp:
            written = await self.ops.copy_from(sid, "/var/lib/migrate", tmp)
            root = written if os.path.isdir(written) else tmp
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                tar.add(root, arcname=f"{ctx.migration['id']}-{stamp}")
            data = buf.getvalue()
        if len(data) > ARTIFACT_LIMIT:
            raise JobFailed(f"artifacts are {len(data) // 1048576} MB, over the {ARTIFACT_LIMIT // 1048576} MB limit")
        aid = await self.store.add_artifact(ctx.migration["id"], f"{ctx.migration['id']}-{stamp}.tar.gz", data)
        await self.note(ctx, f"saved artifacts ({len(data) // 1024} KB) as download #{aid}")

    async def do_snapshot(self, ctx: Ctx, action: Action) -> int:
        sid = await self.require_running_sandbox(ctx)
        name = f"pgm-{ctx.migration['id']}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
        snap = await self.ops.snapshot(sid, name)
        await self.note(ctx, f"runtime snapshot {snap['id']} ({name}) requested; restore with sandboxes create --snapshot-id")
        return 0

    # ------------------------------------------------------- image build
    async def ensure_build_credential(self, ctx: Ctx) -> str:
        """Brokered registry credential on the build group: HTTP Basic
        user:token added to <registry>/token requests only, so the registry's
        token exchange succeeds and nothing else carries the secret."""
        cfg = self.cfg
        gid = await self.ops.ensure_group(cfg.build_group)
        value = f"{cfg.github_username}:{cfg.github_token}"
        fingerprint = hashlib.sha256(f"{gid}\0{cfg.registry_host}\0{value}".encode()).hexdigest()
        cur = await self.ops.get_credential(gid, cfg.credential_id)
        want = {"domain": cfg.registry_host, "paths": ["/token"], "injection": "basic", "enabled": True}
        stale = cur is None or any(cur.get(k) != v for k, v in want.items())
        rotated = (await self.store.get_kv("credential_fingerprint")) != fingerprint
        if stale or rotated:
            verb = "creating" if cur is None else "updating"
            await self.note(ctx, f"{verb} brokered credential '{cfg.credential_id}' on group {cfg.build_group}: "
                                 f"HTTP Basic for https://{cfg.registry_host}/token")
            cur = await self.ops.put_credential(gid, cfg.credential_id, domain=cfg.registry_host, value=value,
                                                paths=["/token"], injection_type="basic", exists=cur is not None)
            await self.store.set_kv("credential_fingerprint", fingerprint)
        else:
            await self.note(ctx, f"brokered credential '{cfg.credential_id}' is current (version {cur.get('version')})")
        await self.store.set_kv("credential", {**(cur or {}), "group": cfg.build_group, "group_id": gid})
        return gid

    def build_context(self) -> str:
        """The files the Dockerfile needs, from this deploy of the repo."""
        root = self.cfg.repo_root
        tmp = tempfile.mkdtemp(prefix="pgm-ctx-")
        for d in ("image", "sandbox", "builder"):
            shutil.copytree(root / d, os.path.join(tmp, d))
        shutil.copy2(root / ".dockerignore", os.path.join(tmp, ".dockerignore"))
        return tmp

    async def do_build(self, ctx: Ctx, action: Action) -> int:
        cfg = self.cfg
        missing = cfg.missing_for("build")
        if missing:
            raise JobFailed("set " + ", ".join(missing) + " on the web service")
        tag, ref = cfg.image_tag, cfg.image_ref()
        d = ctx.data
        if not d.get("build_sandbox_id"):
            await self.store.upsert_image(tag, ref=ref, git_sha=cfg.git_sha, state="building", detail=None,
                                          digest=None, image_id=None)
            gid = await self.ensure_build_credential(ctx)
            await self.note(ctx, f"starting a build sandbox from Render's default image (plan {cfg.build_plan})")
            sb = await self.ops.create_sandbox(group_id=gid, name=f"pg-migrate-build-{tag}", image=None,
                                               plan=cfg.build_plan, env={}, timeout_seconds=7200,
                                               idle_timeout_seconds=0)
            d["build_sandbox_id"] = sb["id"]
            await self.save(ctx)
        sid = d["build_sandbox_id"]
        try:
            if not d.get("build_rc_seen"):
                await self.wait_running(ctx, sid, timeout=600)
                if not d.get("uploaded"):
                    ctxdir = self.build_context()
                    try:
                        await self.ops.copy_to(sid, ctxdir, "/tmp/pgm-build")
                    finally:
                        shutil.rmtree(ctxdir, ignore_errors=True)
                    d["uploaded"] = True
                    await self.save(ctx)
                if not d.get("started"):
                    env = {"IMAGE_REF": ref, "PG_MAJOR": cfg.pg_major, "GIT_SHA": cfg.git_sha,
                           "BASE_IMAGE": cfg.base_image, "BUILDKIT_VERSION": cfg.buildkit_version,
                           "BUILDKIT_SHA256": cfg.buildkit_sha256, "BUILDKIT_BIN_DIR": cfg.buildkit_bin_dir,
                           "REGISTRY_INSECURE": "1" if cfg.registry_insecure else ""}
                    assigns = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items() if v)
                    inner = (f"{assigns} bash /tmp/pgm-build/builder/build_image.sh >/tmp/pgm-build.log 2>&1; "
                             f"echo $? >/tmp/pgm-build.rc")
                    cmd = f"rm -f /tmp/pgm-build.rc; (setsid nohup bash -c {shlex.quote(inner)} >/dev/null 2>&1 </dev/null &)"
                    r = await self.ops.exec(sid, cmd, timeout=60)
                    if not r.ok:
                        raise JobFailed(f"could not start the build: {r.stderr.strip()}")
                    d.update(started=True, offset=0)
                    await self.save(ctx)
                    await self.note(ctx, f"building {ref} in sandbox {sid}")
                rc = await self.follow_file_job(ctx, sid, "/tmp/pgm-build.log", "/tmp/pgm-build.rc")
                d["build_rc_seen"] = True
                d["build_rc"] = rc
                await self.save(ctx)
            if d.get("build_rc") != 0:
                await self.store.upsert_image(tag, ref=ref, state="failed", detail="build failed; see the job log")
                raise JobFailed("the image build failed", d.get("build_rc") or 1)
            row = await self.store.get_job(ctx.job["id"])
            m = re.findall(r"^DIGEST=(sha256:[0-9a-f]+)$", row["log"] if row else "", re.M)
            if not m:
                raise JobFailed("the build reported success but no image digest")
            d["digest"] = m[-1]
            await self.store.upsert_image(tag, ref=ref, digest=d["digest"], state="pushed", detail=None)
        finally:
            if d.get("build_rc_seen") and not d.get("build_sandbox_terminated"):
                try:
                    await self.ops.terminate(sid)
                    await self.note(ctx, f"terminated build sandbox {sid}")
                except Exception as e:  # noqa: BLE001
                    await self.note(ctx, f"WARN could not terminate build sandbox {sid}: {e}")
                d["build_sandbox_terminated"] = True
                await self.save(ctx)
        return await self.import_into_group(ctx, tag)

    async def follow_file_job(self, ctx: Ctx, sid: str, logfile: str, rcfile: str) -> int:
        """Tail a detached command's log until its exit-code file appears.
        The exit code is written after the command's output is closed, so
        draining the log after seeing it gets everything."""
        while True:
            rc = (await self.ops.exec(sid, f"cat {rcfile} 2>/dev/null || true", timeout=60)).stdout.strip()
            off = int(ctx.data.get("offset", 0))
            cmd = (f"s=$(wc -c <{logfile} 2>/dev/null | tr -d ' '); s=${{s:-0}}; echo SIZE=$s; "
                   f"if [ \"$s\" -gt {off} ]; then tail -c +{off + 1} {logfile} | head -c $((s - {off})); fi")
            r = await self.ops.exec(sid, cmd, timeout=90)
            head, _, body = r.stdout.partition("\n")
            if head.startswith("SIZE=") and int(head[5:]) >= off:
                if body:
                    await self.store.append_log(ctx.job["id"], redact(body, ctx.secrets))
                ctx.data["offset"] = int(head[5:])
                await self.save(ctx)
            if rc.isdigit():
                return int(rc)
            await self.check_cancel(ctx)
            await asyncio.sleep(self.cfg.job_poll_seconds)

    async def do_import(self, ctx: Ctx, action: Action) -> int:
        if self.cfg.missing_for("build"):
            raise JobFailed("set " + ", ".join(self.cfg.missing_for("build")) + " on the web service")
        tag = ctx.data.get("tag") or self.cfg.image_tag
        img = await self.store.get_image(tag)
        if not img or not img.get("digest"):
            raise JobFailed(f"image {tag} has not been built and pushed yet")
        ctx.data["digest"] = img["digest"]
        return await self.import_into_group(ctx, tag)

    async def import_into_group(self, ctx: Ctx, tag: str) -> int:
        cfg, d = self.cfg, ctx.data
        source = f"{cfg.github_registry}/{cfg.image_name}@{d['digest']}"
        gid = await self.ops.ensure_group(cfg.sandbox_group)
        if not d.get("image_id"):
            await self.note(ctx, f"importing {source} into sandbox group {cfg.sandbox_group}")
            img = await self.ops.import_image(group_id=gid, name=cfg.image_name, source=source,
                                              username=cfg.github_username, password=cfg.github_token)
            d["image_id"] = img["id"]
            await self.save(ctx)
            await self.store.upsert_image(tag, ref=cfg.image_ref(tag), image_id=img["id"], state="importing")
        loop = asyncio.get_running_loop()
        deadline, last = loop.time() + 3600, None
        while True:
            img = await self.ops.get_image(gid, d["image_id"])
            if img["status"] != last:
                await self.note(ctx, f"image {d['image_id']}: {img['status']}")
                last = img["status"]
            if img["status"] == "succeeded":
                await self.store.upsert_image(tag, ref=cfg.image_ref(tag), image_id=img["id"], state="ready", detail=None)
                await self.note(ctx, f"image {tag} is ready: new sandboxes start from {img['id']}")
                return 0
            if img["status"] == "failed":
                await self.store.upsert_image(tag, ref=cfg.image_ref(tag), state="failed",
                                              detail=img.get("failure_reason") or "import failed")
                raise JobFailed(f"import failed: {img.get('failure_reason') or 'no reason given'}")
            if loop.time() > deadline:
                raise JobFailed("import still running after an hour; rerun Import to keep waiting")
            await self.check_cancel(ctx)
            await asyncio.sleep(10)


async def status_report(cfg: Config, ops: RenderOps, migration: dict) -> tuple[dict, str | None]:
    """What the UI shows as Status: the sandbox, and status.sh --json from inside it."""
    sid = migration["sandbox_id"]
    sb = await ops.get_sandbox(sid)
    report = {"sandbox": sb or {"id": sid, "status": "missing"}, "status": None, "error": None}
    if not sb or sb["status"] != "running":
        report["error"] = f"sandbox is {sb['status'] if sb else 'missing'}"
        return report, None
    r = await ops.exec(sid, "/opt/migrate/status.sh --json", timeout=150)
    try:
        report["status"] = json.loads(r.stdout)
    except ValueError:
        report["error"] = "status.sh failed in the sandbox"
        return report, (r.stderr.strip().splitlines() or ["status.sh failed"])[-1]
    return report, None
