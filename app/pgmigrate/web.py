"""HTTP API and UI. Run with: uvicorn pgmigrate.web:app (see render.yaml)."""

from __future__ import annotations

import contextlib
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .auth import AuthError, Authenticator
from .config import PLANS, Config, allowable_host
from .render_ops import OpsError, RenderOps, SdkOps
from .runbook import ACTIONS, BY_NAME, Engine
from .store import Busy, Store, jsonable
from .vault import SecretBox, summarize
from .worker import Worker

log = logging.getLogger("pgmigrate.web")
STATIC = Path(__file__).parent / "static"
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
SYNC = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
SCHEMAS = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\s*,\s*[A-Za-z_][A-Za-z0-9_$]*)*$")


# --------------------------------------------------------------- validation
def validate_pg_url(raw: str) -> str:
    try:
        u = urlsplit(raw)
        _ = u.port
    except ValueError:
        return "not a valid URL"
    if u.scheme not in ("postgres", "postgresql"):
        return "must start with postgres:// or postgresql://"
    if not u.hostname:
        return "missing host"
    if not u.path.strip("/"):
        return "missing database name"
    if not u.username:
        return "missing user"
    if u.password is None:
        return "missing password"
    return ""


def validate(body: dict, *, require_urls: bool) -> dict:
    errs: dict[str, str] = {}
    for f in ("heroku_url", "render_url"):
        v = body.get(f, "")
        if not v:
            if require_urls:
                errs[f] = "required"
        elif any(c.isspace() for c in v):
            errs[f] = "must not contain whitespace"
        elif msg := validate_pg_url(v):
            errs[f] = msg
    if body.get("heroku_url") and body.get("heroku_url") == body.get("render_url"):
        errs["render_url"] = "must differ from the Heroku URL"
    host = (urlsplit(body.get("render_url", "")).hostname or "") if body.get("render_url") else ""
    if host.startswith("dpg-") and "." not in host:
        errs["render_url"] = "this looks like Render's internal URL; the sandbox needs the external URL (host ends in .render.com)"
    if body.get("sync_name") and not SYNC.match(body["sync_name"]):
        errs["sync_name"] = "letters, digits and underscores; must not start with a digit"
    if body.get("schemas") and not SCHEMAS.match(body["schemas"]):
        errs["schemas"] = "comma-separated schema names"
    if body.get("plan") and body["plan"] not in PLANS:
        errs["plan"] = "one of " + ", ".join(PLANS)
    if body.get("slack_webhook_url") and not body["slack_webhook_url"].startswith("https://"):
        errs["slack_webhook_url"] = "must be an https:// URL"
    # The sandbox may reach only these hosts (its network allow-list), and an
    # allow-list names DNS names, not IP addresses.
    for f in ("heroku_url", "render_url", "slack_webhook_url"):
        if body.get(f) and f not in errs:
            try:
                h = (urlsplit(body[f]).hostname or "").lower()
            except ValueError:
                continue
            if h and not allowable_host(h):
                errs[f] = (f"the host {h} can't go in the sandbox's network allow-list; "
                           f"use the DNS name, not an IP address")
    return errs


def err(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


# ------------------------------------------------------------------ views
def migration_view(m: dict, *, jobs: list | None = None, active: dict | None = None,
                   artifacts: list | None = None) -> dict:
    v = jsonable(m)
    for k in ("status", "status_at", "status_error", "secrets"):
        v.pop(k, None)
    v.update(
        source=m.get("source") or {}, target=m.get("target") or {},
        torn_down=bool(m.get("torn_down_at")), cleanup_done_locally=bool(m.get("cleanup_done_at")),
        active_job=jsonable(active),
    )
    if m.get("status_at") or m.get("status_error"):
        v["status"] = {"at": m["status_at"].isoformat() if m.get("status_at") else None,
                       "report": m.get("status"), "error": m.get("status_error")}
    if jobs is not None:
        v["jobs"] = [jsonable(j) for j in jobs]
    if artifacts is not None:
        v["artifacts"] = [jsonable(a) for a in artifacts]
    return v


class Unconfigured:
    """RenderOps stand-in until RENDER_API_KEY/RENDER_WORKSPACE_ID are set."""

    def __getattr__(self, name):
        async def fail(*a, **k):
            raise OpsError("RENDER_API_KEY and RENDER_WORKSPACE_ID must be set on the web service")
        return fail


# ---------------------------------------------------------------- the app
def create_app(cfg: Config, store: Store | None = None, ops: RenderOps | None = None,
               authenticator: Authenticator | None = None) -> Starlette:
    if authenticator is None and cfg.auth is not None:
        authenticator = Authenticator(cfg.auth)
    if authenticator is None:
        # Strict: on Render, or without dev mode, no identity provider means no app.
        if cfg.on_render or not cfg.dev:
            raise SystemExit(
                "Set AUTH_ISSUER (your identity provider) so every request's access token can be verified. "
                "PGM_DEV=1 turns authentication off for local development, and is ignored on Render.")
        log.warning("PGM_DEV=1 and no AUTH_ISSUER: authentication is OFF. Use this only on 127.0.0.1.")
    else:
        log.info("authentication: %s", authenticator.s.describe())
    store = store or Store(cfg.database_url)
    if ops is None:
        ops = SdkOps(cfg.render_api_key, cfg.render_workspace_id) if not cfg.missing_for("render") else Unconfigured()
    box = SecretBox(cfg.secret_key) if cfg.secret_key else None
    engine = Engine(cfg, store, ops, box)
    worker = Worker(cfg, store, ops, engine)

    def who(request: Request) -> str | None:
        return getattr(request.state, "user", None)

    async def need_migration(request: Request) -> dict | None:
        mid = request.path_params["id"]
        if not ID.match(mid):
            return None
        return await store.get_migration(mid)

    async def body_json(request: Request) -> dict:
        try:
            data = await request.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    # ----------------------------------------------------------- system
    async def system(request: Request) -> JSONResponse:
        cred = await store.get_kv("credential")
        images = [jsonable(i) for i in await store.list_images()]
        latest = await store.latest_ready_image()
        return JSONResponse({
            "app": {"git_sha": cfg.git_sha, "image_tag": cfg.image_tag,
                    "image_ref": cfg.image_ref() if cfg.github_registry else None},
            "checks": cfg.checks(),
            "config": {"GITHUB_REGISTRY": cfg.github_registry, "IMAGE_NAME": cfg.image_name, "PG_MAJOR": cfg.pg_major,
                       "PG_CLIENT_MAJORS": cfg.pg_client_majors,
                       "SANDBOX_GROUP": cfg.sandbox_group, "BUILD_SANDBOX_GROUP": cfg.build_group,
                       "BUILD_SANDBOX_PLAN": cfg.build_plan, "BUILDKIT_VERSION": cfg.buildkit_version,
                       "BUILD_NETWORK_POLICY": cfg.build_network_policy,
                       "BASE_IMAGE": cfg.base_image, "CREDENTIAL_ID": cfg.credential_id,
                       "REGISTRY_HOST": cfg.registry_host},
            "render_ready": not cfg.missing_for("render"),
            "build_ready": not cfg.missing_for("build"),
            "credential": cred,
            "images": images,
            "latest_image": jsonable(latest),
            "plans": PLANS,
            "actions": [a.public() for a in ACTIONS if not a.is_global],
            "active_global_job": jsonable(await store.active_job(None)),
            "worker_leader": worker.is_leader,
        })

    # ------------------------------------------------------- migrations
    async def list_migrations(request: Request) -> JSONResponse:
        out = []
        for m in await store.list_migrations():
            out.append(migration_view(m, active=await store.active_job(m["id"])))
        return JSONResponse(out)

    async def create_migration(request: Request) -> JSONResponse:
        if box is None:
            return err(503, "APP_SECRET_KEY is not set on the web service")
        b = {k: (v.strip() if isinstance(v, str) else v) for k, v in (await body_json(request)).items()}
        errs = validate(b, require_urls=True)
        mid = b.get("id", "")
        if not ID.match(mid):
            errs["id"] = "1-40 lowercase letters, digits or dashes; start with a letter or digit"
        elif await store.get_migration(mid):
            errs["id"] = "a migration with this name already exists"
        if errs:
            return err(400, "invalid input", fields=errs)
        sealed = box.seal({"heroku_url": b["heroku_url"], "render_url": b["render_url"],
                           "slack_webhook_url": b.get("slack_webhook_url", "")})
        await store.create_migration(
            id=mid, title=b.get("title") or mid, secrets=sealed,
            sync_name=b.get("sync_name") or "migrate_" + mid.replace("-", "_"),
            schemas=(b.get("schemas") or "public").replace(" ", ""), plan=b.get("plan") or cfg.default_plan,
            source=summarize(b["heroku_url"]), target=summarize(b["render_url"]),
            has_slack=bool(b.get("slack_webhook_url")), created_by=who(request))
        log.info("created migration %s (by %s)", mid, who(request))
        return JSONResponse(migration_view(await store.get_migration(mid), jobs=[], artifacts=[]), status_code=201)

    async def get_migration(request: Request) -> JSONResponse:
        m = await need_migration(request)
        if not m:
            return err(404, "no such migration")
        v = migration_view(m, jobs=await store.list_jobs(m["id"]), active=await store.active_job(m["id"]),
                           artifacts=await store.list_artifacts(m["id"]))
        v["sync_started"] = await store.has_job(m["id"], "start_sync")
        return JSONResponse(v)

    async def update_migration(request: Request) -> JSONResponse:
        m = await need_migration(request)
        if not m:
            return err(404, "no such migration")
        b = {k: (v.strip() if isinstance(v, str) else v) for k, v in (await body_json(request)).items()}
        settings = {k: b[k] for k in ("heroku_url", "render_url", "sync_name", "schemas", "plan", "slack_webhook_url") if b.get(k)}
        if settings:
            if m.get("sandbox_id") and not m.get("torn_down_at"):
                return err(409, "settings are locked while a sandbox exists: it keeps the values it was created with. Tear it down first.")
            errs = validate(settings, require_urls=False)
            if errs:
                return err(400, "invalid input", fields=errs)
            if box is None:
                return err(503, "APP_SECRET_KEY is not set on the web service")
            secrets = box.open(m["secrets"])
            for k in ("heroku_url", "render_url", "slack_webhook_url"):
                if settings.get(k):
                    secrets[k] = settings[k]
            fields = {"secrets": box.seal(secrets), "source": summarize(secrets["heroku_url"]),
                      "target": summarize(secrets["render_url"]), "has_slack": bool(secrets.get("slack_webhook_url"))}
            for k in ("sync_name", "plan"):
                if settings.get(k):
                    fields[k] = settings[k]
            if settings.get("schemas"):
                fields["schemas"] = settings["schemas"].replace(" ", "")
            await store.update_migration(m["id"], **fields)
        if isinstance(b.get("title"), str):
            await store.update_migration(m["id"], title=b["title"] or m["id"])
        m = await store.get_migration(m["id"])
        return JSONResponse(migration_view(m, jobs=await store.list_jobs(m["id"]), artifacts=await store.list_artifacts(m["id"])))

    async def delete_migration(request: Request) -> Response:
        m = await need_migration(request)
        if not m:
            return err(404, "no such migration")
        if (await body_json(request)).get("confirm") != m["id"]:
            return err(400, "type the migration name to confirm")
        if m.get("sandbox_id") and not m.get("torn_down_at"):
            return err(409, "this migration still has a sandbox; tear it down first")
        if await store.active_job(m["id"]):
            return err(409, "a job is running")
        await store.delete_migration(m["id"])
        log.info("deleted migration %s (by %s)", m["id"], who(request))
        return Response(status_code=204)

    async def refresh(request: Request) -> JSONResponse:
        m = await need_migration(request)
        if not m:
            return err(404, "no such migration")
        worker.request_refresh(m["id"])
        return JSONResponse({"status": "refreshing"}, status_code=202)

    async def run_action(request: Request) -> JSONResponse:
        m = await need_migration(request)
        if not m:
            return err(404, "no such migration")
        a = BY_NAME.get(request.path_params["action"])
        if not a or a.is_global:
            return err(404, "no such action")
        b = await body_json(request)
        if a.destructive and b.get("confirm") != m["id"]:
            return err(400, "type the migration name to confirm")
        data = {}
        if a.name == "teardown" and b.get("force"):
            # Skipping the Heroku cleanup check is safe only if Bucardo never
            # touched Heroku, which only Start sync does.
            if await store.has_job(m["id"], "start_sync"):
                return err(409, "a sync was started on this migration, so Bucardo may have objects on Heroku: "
                                "run Clean up Heroku before tearing down")
            data = {"force": True}
        try:
            job = await store.create_job(migration_id=m["id"], action=a.name, label=a.label, read_only=a.read_only,
                                         data=data, started_by=who(request))
        except Busy as e:
            return err(409, str(e))
        log.info("migration %s: queued %s (%s) by %s", m["id"], a.name, job["id"], who(request))
        return JSONResponse(jsonable(job), status_code=202)

    async def job_log(request: Request) -> JSONResponse:
        mid = request.path_params.get("id")
        job = await store.get_job(request.path_params["job"])
        if not job or job["migration_id"] != mid:
            return err(404, "no such job")
        try:
            offset = int(request.query_params.get("offset", "0"))
        except ValueError:
            offset = 0
        text, n = await store.read_log(job["id"], offset)
        job.pop("log", None)
        return JSONResponse({"job": jsonable(job), "text": text, "next_offset": n})

    async def cancel_job(request: Request) -> JSONResponse:
        mid = request.path_params.get("id")
        job = await store.get_job(request.path_params["job"])
        if not job or job["migration_id"] != mid or job["state"] not in ("queued", "running"):
            return err(409, "job is not running")
        await store.set_job(job["id"], data={**job["data"], "cancel": True})
        return JSONResponse({"status": "cancelling"}, status_code=202)

    async def artifact(request: Request) -> Response:
        m = await need_migration(request)
        if not m:
            return err(404, "no such migration")
        try:
            row = await store.get_artifact(m["id"], int(request.path_params["aid"]))
        except ValueError:
            row = None
        if not row:
            return err(404, "no such artifact")
        return Response(bytes(row["content"]), media_type="application/gzip",
                        headers={"Content-Disposition": f'attachment; filename="{row["name"]}"'})

    # ----------------------------------------------------------- global
    async def global_action(request: Request) -> JSONResponse:
        a = BY_NAME.get(request.path_params["action"])
        if not a or not a.is_global:
            return err(404, "no such action")
        b = await body_json(request)
        data = {"tag": b["tag"]} if a.name == "import_image" and isinstance(b.get("tag"), str) else {}
        try:
            job = await store.create_job(migration_id=None, action=a.name, label=a.label, read_only=False, data=data,
                                         started_by=who(request))
        except Busy as e:
            return err(409, str(e))
        log.info("queued global %s (%s) by %s", a.name, job["id"], who(request))
        return JSONResponse(jsonable(job), status_code=202)

    async def global_jobs(request: Request) -> JSONResponse:
        return JSONResponse({"jobs": [jsonable(j) for j in await store.list_jobs(None, 15)],
                             "active": jsonable(await store.active_job(None))})

    async def me(request: Request) -> JSONResponse:
        user = who(request)
        name = None
        if authenticator is not None and getattr(request.state, "claims", None):
            name = await authenticator.display_name(request.state.token, request.state.claims)
        return JSONResponse({"user": user, "name": name,
                             "issuer": authenticator.s.issuer if authenticator else None,
                             "authenticated": authenticator is not None})

    async def healthz(request: Request) -> Response:
        return Response("ok")

    async def index(request: Request) -> FileResponse:
        return FileResponse(STATIC / "index.html")

    routes = [
        Route("/healthz", healthz),
        Route("/", index),
        Route("/api/system", system),
        Route("/api/me", me),
        Route("/api/migrations", list_migrations, methods=["GET"]),
        Route("/api/migrations", create_migration, methods=["POST"]),
        Route("/api/migrations/{id}", get_migration, methods=["GET"]),
        Route("/api/migrations/{id}", update_migration, methods=["PATCH"]),
        Route("/api/migrations/{id}", delete_migration, methods=["DELETE"]),
        Route("/api/migrations/{id}/refresh", refresh, methods=["POST"]),
        Route("/api/migrations/{id}/actions/{action}", run_action, methods=["POST"]),
        Route("/api/migrations/{id}/jobs/{job}/log", job_log),
        Route("/api/migrations/{id}/jobs/{job}/cancel", cancel_job, methods=["POST"]),
        Route("/api/migrations/{id}/artifacts/{aid}", artifact),
        Route("/api/global/actions/{action}", global_action, methods=["POST"]),
        Route("/api/global/jobs", global_jobs),
        Route("/api/global/jobs/{job}/log", job_log),
    ]
    fonts = cfg.repo_root / "app" / "fonts"
    if fonts.is_dir():
        routes.append(Mount("/fonts", StaticFiles(directory=fonts)))
    routes.append(Mount("/", StaticFiles(directory=STATIC)))

    @contextlib.asynccontextmanager
    async def lifespan(app):
        await store.open()
        await worker.start()
        try:
            yield
        finally:
            await worker.stop()
            await store.close()

    app = Starlette(routes=routes, lifespan=lifespan,
                    middleware=[Middleware(Guard, authenticator=authenticator)])
    app.state.cfg, app.state.store, app.state.worker, app.state.engine = cfg, store, worker, engine
    app.state.authenticator = authenticator
    return app


class Guard(BaseHTTPMiddleware):
    """Strict authentication, then cross-site request checks.

    * Every request except /healthz must carry a valid access token (JWT) in
      the configured header, verified against the identity provider's keys
      (auth.py). A request without one is refused even if it reached the app
      some other way, e.g. over Render's private network.
    * Being signed in doesn't stop cross-site request forgery: another site
      could make a signed-in browser send a POST, and the platform would add
      the token. So state-changing requests must also be JSON with
      X-PGM-Request: 1 and, if the browser sends one, a same-origin Origin.
    """

    def __init__(self, app, authenticator: Authenticator | None):
        super().__init__(app)
        self.auth = authenticator

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        if self.auth is None:  # PGM_DEV=1 off Render only (create_app enforces this)
            request.state.user = "dev"
        else:
            raw = request.headers.get(self.auth.header)
            try:
                claims = await self.auth.verify(raw)
            except AuthError as e:
                hint = ""
                if self.auth.s.verify == "jwt" and str(e) == "access token is not a JWT":
                    hint = ("; if your provider sends opaque or encrypted access tokens (Auth0 does unless "
                            "an API audience is requested), set AUTH_VERIFY=userinfo")
                log.warning("refused %s %s: %s (%d). Token: %s. Auth headers present: %s%s",
                            request.method, request.url.path, e, e.status, self.auth.describe_token(raw),
                            auth_header_names(request) or "none", hint)
                return err(e.status, str(e))
            request.state.user = self.auth.user_of(claims)
            request.state.claims = claims
            request.state.token = raw
        if request.method not in ("GET", "HEAD"):
            if request.headers.get("x-pgm-request") != "1" or \
                    not request.headers.get("content-type", "").startswith("application/json"):
                return err(403, "missing request header")
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc.lower() != (request.headers.get("host") or "").lower():
                return err(403, "cross-origin request refused")
        resp = await call_next(request)
        h = resp.headers
        h["X-Content-Type-Options"] = "nosniff"
        h["Referrer-Policy"] = "no-referrer"
        h["X-Frame-Options"] = "DENY"
        h["Cache-Control"] = "no-store"
        h["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
            h["Strict-Transport-Security"] = "max-age=31536000"
        return resp


_NOT_AUTH = {"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "x-forwarded-port"}


def auth_header_names(request: Request) -> str:
    """Names (never values) of the headers a login proxy might use, for logs."""
    names = sorted({k.lower() for k in request.headers.keys()
                    if k.lower() in ("authorization", "gap-auth")
                    or (k.lower().startswith(("x-forwarded-", "x-auth-request-")) and k.lower() not in _NOT_AUTH)})
    return ", ".join(names)


def app_from_env() -> Starlette:
    """Entry point: uvicorn --factory pgmigrate.web:app_from_env"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return create_app(Config.from_env())
