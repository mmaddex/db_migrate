"""Everything the app asks of Render, behind one small interface.

`SdkOps` uses the Render Python SDK (`render` package). Tests swap in a
Docker-backed implementation with the same methods (tests/docker_ops.py).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from .config import parse_network_policy


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class OpsError(Exception):
    """A Render API call failed. The message is safe to show."""


# Sandbox states that come back to running on their own or with wake():
# Render suspends an idle sandbox and resumes it on its next command.
ASLEEP = ("suspending", "suspended", "resuming")
# States a sandbox can still be used from, given time or a wake().
ALIVE = ("creating", "running", *ASLEEP)


class RenderOps(Protocol):
    async def ensure_group(self, name: str) -> str: ...
    async def get_credential(self, group_id: str, credential_id: str) -> dict | None: ...
    async def put_credential(self, group_id: str, credential_id: str, *, domain: str, value: str | None,
                             paths: list[str], injection_type: str, exists: bool) -> dict: ...
    async def create_sandbox(self, *, group_id: str, name: str, image: str | None, plan: str,
                             env: dict[str, str], timeout_seconds: int, idle_timeout_seconds: int,
                             network_policy: str) -> dict: ...
    async def get_sandbox(self, sandbox_id: str) -> dict | None: ...
    async def find_sandbox(self, group_id: str, name: str) -> dict | None: ...
    async def terminate(self, sandbox_id: str) -> None: ...
    async def wake(self, sandbox_id: str) -> None: ...
    async def exec(self, sandbox_id: str, command: str, timeout: float = 120) -> ExecResult: ...
    async def copy_to(self, sandbox_id: str, local_path: str, remote_path: str) -> None: ...
    async def copy_from(self, sandbox_id: str, remote_path: str, local_path: str) -> str: ...
    async def import_image(self, *, group_id: str, name: str, source: str, username: str, password: str) -> dict: ...
    async def get_image(self, group_id: str, image_id: str) -> dict: ...
    async def snapshot(self, sandbox_id: str, name: str) -> dict: ...


def sdk_network_policy(value: str):
    """The SDK's NetworkPolicy for a policy value (see config.parse_network_policy)."""
    from render.sandbox import EgressRule, NetworkPolicy

    kind, hosts = parse_network_policy(value)
    return NetworkPolicy(type=kind, rules=[EgressRule(domain=h) for h in hosts])


CREDENTIAL_ATTEMPTS = 3
CREDENTIAL_RETRY_SECONDS = 2.0


def credential_refused(status: int, group_id: str, workspace_id: str) -> str:
    if status == 403:
        return (f"Render refused to save the brokered credential on sandbox group {group_id} (403 Forbidden). "
                f"Creating or changing credentials needs the API key of a workspace admin: set RENDER_API_KEY "
                f"to an admin's key for workspace {workspace_id}.")
    return (f"Render answered 404 Not Found when saving the brokered credential on sandbox group {group_id}, "
            f"which exists. Credential brokering is in alpha and is switched on per workspace, so it is "
            f"probably not enabled for workspace {workspace_id}. Ask Render to enable it, then run "
            f"Build & push image again; the sandbox group is reused.")


def _sandbox_dict(sb) -> dict:
    return {
        "id": sb.id, "name": getattr(sb, "name", None), "status": str(getattr(sb.status, "value", sb.status)),
        "plan": str(getattr(sb.plan, "value", sb.plan)) if getattr(sb, "plan", None) else None,
        "sandbox_group_id": getattr(sb, "sandbox_group_id", None),
        "idle_timeout_seconds": getattr(sb, "idle_timeout_seconds", None),
        "network_policy": getattr(getattr(sb, "network_policy", None), "type", None),
        "created_at": sb.created_at.isoformat() if getattr(sb, "created_at", None) else None,
    }


class SdkOps:
    """RenderOps over the Render Python SDK (async client)."""

    def __init__(self, api_key: str, workspace_id: str):
        from render import RenderAsync

        self._render = RenderAsync(token=api_key, owner_id=workspace_id)
        self.sb = self._render.sandboxes
        self.workspace_id = workspace_id
        self._groups: dict[str, str] = {}

    async def ensure_group(self, name: str) -> str:
        if name in self._groups:
            return self._groups[name]
        cursor = None
        while True:
            page = await self.sb.list_groups(cursor=cursor, limit=100)
            for g in page.groups:
                if g.name == name:
                    self._groups[name] = g.id
                    return g.id
            cursor = page.next_cursor
            if not cursor:
                break
        g = await self.sb.create_group(name)
        self._groups[name] = g.id
        return g.id

    async def get_credential(self, group_id: str, credential_id: str) -> dict | None:
        from render.experimental.sandbox import CredentialNotFoundError

        try:
            c = await self.sb.credentials.from_id(sandbox_group_id=group_id, credential_id=credential_id)
        except CredentialNotFoundError:
            return None
        inj = c.injection
        return {"id": c.id, "domain": c.domain, "enabled": c.enabled, "paths": list(c.paths or []),
                "methods": list(c.methods or []), "injection": getattr(inj, "type", None),
                "version": c.version.id if c.version is not None else None,
                "updated_at": c.version.updated_at.isoformat() if c.version is not None else None}

    async def put_credential(self, group_id, credential_id, *, domain, value, paths, injection_type, exists) -> dict:
        from render.client.errors import ClientError
        from render.experimental.sandbox import CredentialInjection

        inj = CredentialInjection(type=injection_type, name=None, template=None)
        call = self.sb.credentials.update if exists else self.sb.credentials.create
        for attempt in range(CREDENTIAL_ATTEMPTS):
            try:
                await call(sandbox_group_id=group_id, credential_id=credential_id, domain=domain,
                           value=value, injection=inj, paths=paths, enabled=True)
                break
            except ClientError as e:
                if e.status_code == 404 and attempt + 1 < CREDENTIAL_ATTEMPTS:
                    await asyncio.sleep(CREDENTIAL_RETRY_SECONDS)  # a group created a moment ago may not be visible yet
                    continue
                if e.status_code in (403, 404):
                    raise OpsError(credential_refused(e.status_code, group_id, self.workspace_id)) from e
                raise
        return await self.get_credential(group_id, credential_id) or {}

    async def create_sandbox(self, *, group_id, name, image, plan, env, timeout_seconds, idle_timeout_seconds,
                             network_policy) -> dict:
        sb = await self.sb.create(name=name, image=image, plan=plan, env=env, timeout_seconds=timeout_seconds,
                                  idle_timeout_seconds=idle_timeout_seconds, sandbox_group_id=group_id,
                                  network_policy=sdk_network_policy(network_policy))
        return _sandbox_dict(sb)

    async def get_sandbox(self, sandbox_id: str) -> dict | None:
        from render.sandbox import SandboxNotFoundError

        try:
            return _sandbox_dict(await self.sb.from_id(sandbox_id))
        except SandboxNotFoundError:
            return None

    async def find_sandbox(self, group_id: str, name: str) -> dict | None:
        # A suspended sandbox is still this migration's: reuse it, don't start a second.
        page = await self.sb.list(name=name, sandbox_group_id=group_id, status=list(ALIVE))
        items = sorted(page.sandboxes, key=lambda s: s.created_at)
        return _sandbox_dict(items[-1]) if items else None

    async def terminate(self, sandbox_id: str) -> None:
        await self.sb.terminate(sandbox_id)

    async def wake(self, sandbox_id: str) -> None:
        """Resume a suspended sandbox. Render has no resume call; a sandbox
        resumes on its next command or file transfer, so run a no-op."""
        await self.exec(sandbox_id, "true", timeout=300)

    async def exec(self, sandbox_id: str, command: str, timeout: float = 120) -> ExecResult:
        from render.sandbox import SandboxExecExit, SandboxExecOutput

        out, err, code = [], [], None

        async def run():
            nonlocal code
            async for ev in self.sb.exec(sandbox_id, command):
                if isinstance(ev, SandboxExecOutput):
                    (err if ev.stream == "stderr" else out).append(ev.data)
                elif isinstance(ev, SandboxExecExit):
                    code = ev.exit_code

        try:
            await asyncio.wait_for(run(), timeout)
        except asyncio.TimeoutError as e:
            raise OpsError(f"command timed out after {timeout:.0f}s: {command[:80]}") from e
        return ExecResult(code if code is not None else -1, "".join(out), "".join(err))

    async def copy_to(self, sandbox_id: str, local_path: str, remote_path: str) -> None:
        await self.sb.copy_to(sandbox_id, local_path, remote_path)

    async def copy_from(self, sandbox_id: str, remote_path: str, local_path: str) -> str:
        return await self.sb.copy_from(sandbox_id, remote_path, local_path)

    async def import_image(self, *, group_id, name, source, username, password) -> dict:
        from render.sandbox import RegistryCredentials

        creds = RegistryCredentials(username=username, password=password) if username else None
        img = await self.sb.images.import_(sandbox_group_id=group_id, name=name, source=source,
                                           registry_credentials=creds)
        return _image_dict(img)

    async def get_image(self, group_id: str, image_id: str) -> dict:
        return _image_dict(await self.sb.images.from_id(sandbox_group_id=group_id, image_id=image_id))

    async def snapshot(self, sandbox_id: str, name: str) -> dict:
        snap = await self.sb.snapshots.create(sandbox_id, kind="runtime", name=name)
        return {"id": snap.id, "status": snap.status, "name": snap.name}


def _image_dict(img) -> dict:
    return {"id": img.id, "name": img.name, "status": str(getattr(img.status, "value", img.status)),
            "digest": getattr(img, "source_digest", None), "failure_reason": getattr(img, "failure_reason", None)}
