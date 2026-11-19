"""Settings, all from environment variables (set on the Render web service)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit
from pathlib import Path

from .auth import AuthSettings, settings_from_env

# Same Debian base digest as image/Dockerfile, pulled through Google's Docker
# Hub mirror so sandbox builds don't hit Docker Hub's anonymous rate limits.
DEFAULT_BASE_IMAGE = (
    "mirror.gcr.io/library/debian:trixie-slim"
    "@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2"
)

PLANS = ["1c-2g", "2c-4g", "4c-8g", "8c-16g"]

# Postgres majors whose client tools go into the image (image/Dockerfile's
# default): the versions a migration's source and target can have. Preflight
# picks the pg_dump to use (pick_client_major in sandbox/lib.sh).
DEFAULT_PG_CLIENT_MAJORS = "14 15 16 17 18"


def parse_pg_majors(value: str, setting: str = "PG_CLIENT_MAJORS") -> str:
    """Space-separated major versions from a comma or space list; raises ValueError."""
    majors = [m for m in re.split(r"[\s,]+", value.strip()) if m]
    if not majors:
        raise ValueError(f"{setting} is empty: list Postgres major versions, e.g. {DEFAULT_PG_CLIENT_MAJORS}")
    bad = [m for m in majors if not m.isdigit()]
    if bad:
        raise ValueError(f"{setting}: {', '.join(bad)} isn't a Postgres major version (e.g. 17)")
    return " ".join(sorted(dict.fromkeys(majors), key=int))

# Every host the image build contacts, all over HTTPS: BuildKit's GitHub
# release and the host it redirects to, the base image's registry, Debian and
# PGDG apt, and the registry the image is pushed to.
DEFAULT_BUILD_NETWORK_POLICY = ",".join([
    "github.com", "release-assets.githubusercontent.com", "mirror.gcr.io",
    "deb.debian.org", "apt.postgresql.org", "ghcr.io",
])

# A sandbox's outbound network policy, as written in BUILD_NETWORK_POLICY:
# allow-all, deny-all, or a comma list of hosts (an allow-list; *.example.com
# covers subdomains). Migration sandboxes get an allow-list made from their
# migration's URLs (migration_network_policy).
NETWORK_POLICIES = ("allow-all", "deny-all")
MAX_EGRESS_RULES = 100
_HOST = re.compile(r"^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_IPV4 = re.compile(r"^[0-9.]+$")

# Hosts every migration sandbox needs besides its two databases: Diagnostics
# asks this service for the sandbox's outbound IP (sandbox/diagnose.sh).
MIGRATION_EXTRA_HOSTS = ("api.ipify.org",)


def allowable_host(host: str) -> bool:
    """A host Render's allow-list can name: a DNS name, not an IP address."""
    return bool(_HOST.match(host)) and not _IPV4.match(host)


def parse_network_policy(value: str, setting: str = "network policy") -> tuple[str, tuple[str, ...]]:
    """(policy type, allowed hosts) from a setting's value; raises ValueError."""
    v = value.strip().lower()
    if v in NETWORK_POLICIES:
        return v, ()
    hosts = tuple(dict.fromkeys(h.strip() for h in v.split(",") if h.strip()))
    if not hosts:
        raise ValueError(f"{setting} is empty: use allow-all, deny-all, or a comma list of HTTPS hosts")
    bad = [h for h in hosts if not allowable_host(h)]
    if bad:
        raise ValueError(f"{setting}: {', '.join(bad)} isn't a hostname. Use bare DNS names such as github.com, "
                         f"not IP addresses, without a scheme, port or path; a wildcard is allowed only as the "
                         f"first label (*.example.com)")
    if len(hosts) > MAX_EGRESS_RULES:
        raise ValueError(f"{setting} lists {len(hosts)} hosts; Render allows at most {MAX_EGRESS_RULES}")
    return "allow-list", hosts


def migration_network_policy(heroku_url: str, render_url: str, slack_webhook_url: str = "") -> str:
    """The allow-list for a migration's sandbox: the hosts of its two
    databases, its Slack webhook if it has one, and MIGRATION_EXTRA_HOSTS.
    Raises ValueError if a host can't be allow-listed (an IP address)."""
    hosts = [urlsplit(heroku_url).hostname or "", urlsplit(render_url).hostname or ""]
    if slack_webhook_url:
        hosts.append(urlsplit(slack_webhook_url).hostname or "")
    value = ",".join([*(h.lower() for h in hosts), *MIGRATION_EXTRA_HOSTS])
    kind, allowed = parse_network_policy(value, "the migration's network allow-list")
    return ",".join(allowed)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Config:
    # Render API (sandboxes, images, credential brokering).
    render_api_key: str = ""
    render_workspace_id: str = ""
    # The web app's own state.
    database_url: str = ""
    # Every request must carry a valid token from this identity provider
    # (see auth.py). Render's service authentication forwards it.
    auth: AuthSettings | None = None
    on_render: bool = False
    # Encrypts stored connection strings. Derived into a Fernet key.
    secret_key: str = ""
    # GitHub container registry, e.g. ghcr.io/acme. The image is
    # <registry>/<image_name>:<git sha>.
    github_registry: str = ""
    github_username: str = ""
    github_token: str = ""
    image_name: str = "pg-migrate"
    # Major of the image's local Postgres, which holds Bucardo's control
    # database. The migrated databases' versions are PG_CLIENT_MAJORS'.
    pg_major: str = "17"
    pg_client_majors: str = DEFAULT_PG_CLIENT_MAJORS
    base_image: str = DEFAULT_BASE_IMAGE
    buildkit_version: str = "v0.35.0"
    buildkit_sha256: str = ""
    # Sandbox groups: migrations run in one; image builds run in another that
    # holds the brokered registry credential (least privilege).
    sandbox_group: str = "pg-migration"
    build_group: str = "pg-migration-build"
    build_plan: str = "4c-8g"
    default_plan: str = "2c-4g"
    # Outbound network policy of the build sandbox (see parse_network_policy).
    # Migration sandboxes get theirs from their URLs (migration_network_policy).
    build_network_policy: str = DEFAULT_BUILD_NETWORK_POLICY
    credential_id: str = "registry-token"
    git_sha: str = "dev"
    poll_seconds: float = 30.0
    job_poll_seconds: float = 3.0
    repo_root: Path = Path(__file__).resolve().parents[2]
    dev: bool = False
    # Tests only: BuildKit binaries already present in the build sandbox.
    buildkit_bin_dir: str = ""
    registry_insecure: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            render_api_key=_env("RENDER_API_KEY"),
            render_workspace_id=_env("RENDER_WORKSPACE_ID"),
            database_url=_env("DATABASE_URL"),
            on_render=_env("RENDER") == "true",
            auth=settings_from_env(_env),
            secret_key=_env("APP_SECRET_KEY"),
            github_registry=_env("GITHUB_REGISTRY").rstrip("/"),
            github_username=_env("GITHUB_USERNAME"),
            github_token=_env("GITHUB_TOKEN"),
            image_name=_env("IMAGE_NAME", "pg-migrate"),
            pg_major=_env("PG_MAJOR", "17"),
            pg_client_majors=parse_pg_majors(_env("PG_CLIENT_MAJORS", DEFAULT_PG_CLIENT_MAJORS)),
            base_image=_env("BASE_IMAGE", DEFAULT_BASE_IMAGE),
            buildkit_version=_env("BUILDKIT_VERSION", "v0.35.0"),
            buildkit_sha256=_env("BUILDKIT_SHA256"),
            sandbox_group=_env("SANDBOX_GROUP", "pg-migration"),
            build_group=_env("BUILD_SANDBOX_GROUP", "pg-migration-build"),
            build_plan=_env("BUILD_SANDBOX_PLAN", "4c-8g"),
            default_plan=_env("SANDBOX_PLAN", "2c-4g"),
            build_network_policy=_env("BUILD_NETWORK_POLICY", DEFAULT_BUILD_NETWORK_POLICY),
            git_sha=(_env("RENDER_GIT_COMMIT") or _env("GIT_SHA") or "dev"),
            poll_seconds=float(_env("STATUS_POLL_SECONDS", "30")),
            dev=_env("PGM_DEV") == "1",
        )

    def __post_init__(self):
        # Fail at startup, not when a sandbox is created.
        parse_network_policy(self.build_network_policy, "BUILD_NETWORK_POLICY")
        parse_pg_majors(self.pg_client_majors)

    # ------------------------------------------------------------ derived
    @property
    def registry_host(self) -> str:
        return self.github_registry.split("/", 1)[0].lower() if self.github_registry else ""

    @property
    def image_tag(self) -> str:
        return self.git_sha[:12]

    def image_ref(self, tag: str | None = None) -> str:
        return f"{self.github_registry}/{self.image_name}:{tag or self.image_tag}"

    def checks(self) -> list[dict]:
        """What the Image & environment page shows: which settings are set."""
        def item(name: str, ok: bool, detail: str) -> dict:
            return {"name": name, "ok": ok, "detail": detail}

        return [
            item("RENDER_API_KEY", bool(self.render_api_key), "Render API key for sandboxes and images"),
            item("RENDER_WORKSPACE_ID", bool(self.render_workspace_id), "Workspace (owner) ID the sandboxes belong to"),
            item("GITHUB_REGISTRY", bool(self.github_registry), f"Image goes to {self.image_ref() if self.github_registry else '<registry>/' + self.image_name}"),
            item("GITHUB_USERNAME", bool(self.github_username), "Registry user for the brokered credential and image import"),
            item("GITHUB_TOKEN", bool(self.github_token), "Token with write:packages (never sent to the build sandbox)"),
            item("APP_SECRET_KEY", bool(self.secret_key), "Encrypts stored connection strings"),
        ]

    def missing_for(self, what: str) -> list[str]:
        need = {
            "render": ["RENDER_API_KEY", "RENDER_WORKSPACE_ID"],
            "build": ["RENDER_API_KEY", "RENDER_WORKSPACE_ID", "GITHUB_REGISTRY", "GITHUB_USERNAME", "GITHUB_TOKEN"],
        }[what]
        have = {c["name"]: c["ok"] for c in self.checks()}
        return [n for n in need if not have.get(n)]
