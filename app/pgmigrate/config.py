"""Settings, all from environment variables (set on the Render web service)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Same Debian base digest as image/Dockerfile, pulled through Google's Docker
# Hub mirror so sandbox builds don't hit Docker Hub's anonymous rate limits.
DEFAULT_BASE_IMAGE = (
    "mirror.gcr.io/library/debian:trixie-slim"
    "@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2"
)

PLANS = ["1c-2g", "2c-4g", "4c-8g", "8c-16g"]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Config:
    # Render API (sandboxes, images, credential brokering).
    render_api_key: str = ""
    render_workspace_id: str = ""
    # The web app's own state.
    database_url: str = ""
    # The app has no login of its own: Render's service authentication
    # (`authentication: enabled: true` in render.yaml) sits in front of it.
    # It refuses to start unless it is on Render (RENDER=true) or in dev mode.
    on_render: bool = False
    # Encrypts stored connection strings. Derived into a Fernet key.
    secret_key: str = ""
    # GitHub container registry, e.g. ghcr.io/acme. The image is
    # <registry>/<image_name>:<git sha>.
    github_registry: str = ""
    github_username: str = ""
    github_token: str = ""
    image_name: str = "pg-migrate"
    pg_major: str = "17"
    base_image: str = DEFAULT_BASE_IMAGE
    buildkit_version: str = "v0.35.0"
    buildkit_sha256: str = ""
    # Sandbox groups: migrations run in one; image builds run in another that
    # holds the brokered registry credential (least privilege).
    sandbox_group: str = "pg-migration"
    build_group: str = "pg-migration-build"
    build_plan: str = "4c-8g"
    default_plan: str = "2c-4g"
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
            secret_key=_env("APP_SECRET_KEY"),
            github_registry=_env("GITHUB_REGISTRY").rstrip("/"),
            github_username=_env("GITHUB_USERNAME"),
            github_token=_env("GITHUB_TOKEN"),
            image_name=_env("IMAGE_NAME", "pg-migrate"),
            pg_major=_env("PG_MAJOR", "17"),
            base_image=_env("BASE_IMAGE", DEFAULT_BASE_IMAGE),
            buildkit_version=_env("BUILDKIT_VERSION", "v0.35.0"),
            buildkit_sha256=_env("BUILDKIT_SHA256"),
            sandbox_group=_env("SANDBOX_GROUP", "pg-migration"),
            build_group=_env("BUILD_SANDBOX_GROUP", "pg-migration-build"),
            build_plan=_env("BUILD_SANDBOX_PLAN", "4c-8g"),
            default_plan=_env("SANDBOX_PLAN", "2c-4g"),
            git_sha=(_env("RENDER_GIT_COMMIT") or _env("GIT_SHA") or "dev"),
            poll_seconds=float(_env("STATUS_POLL_SECONDS", "30")),
            dev=_env("PGM_DEV") == "1",
        )

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
