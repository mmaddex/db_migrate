"""Everything the app asks of Render, behind one small interface.

`SdkOps` uses the Render Python SDK (`render` package). Tests swap in a
Docker-backed implementation with the same methods (tests/docker_ops.py).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol


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


class RenderOps(Protocol):
    async def ensure_group(self, name: str) -> str: ...
    async def get_credential(self, group_id: str, credential_id: str) -> dict | None: ...
    async def put_credential(self, group_id: str, credential_id: str, *, domain: str, value: str | None,
                             paths: list[str], injection_type: str, exists: bool) -> dict: ...
    async def create_sandbox(self, *, group_id: str, name: str, image: str | None, plan: str,
                             env: dict[str, str], timeout_seconds: int, idle_timeout_seconds: int) -> dict: ...
    async def get_sandbox(self, sandbox_id: str) -> dict | None: ...
    async def find_sandbox(self, group_id: str, name: str) -> dict | None: ...
    async def terminate(self, sandbox_id: str) -> None: ...
    async def exec(self, sandbox_id: str, command: str, timeout: float = 120) -> ExecResult: ...
    async def copy_to(self, sandbox_id: str, local_path: str, remote_path: str) -> None: ...
    async def copy_from(self, sandbox_id: str, remote_path: str, local_path: str) -> str: ...
    async def import_image(self, *, group_id: str, name: str, source: str, username: str, password: str) -> dict: ...
    async def get_image(self, group_id: str, image_id: str) -> dict: ...
    async def snapshot(self, sandbox_id: str, name: str) -> dict: ...


def _sandbox_dict(sb) -> dict:
    return {
        "id": sb.id, "name": getattr(sb, "name", None), "status": str(getattr(sb.status, "value", sb.status)),
        "plan": str(getattr(sb.plan, "value", sb.plan)) if getattr(sb, "plan", None) else None,
        "sandbox_group_id": getattr(sb, "sandbox_group_id", None),
        "created_at": sb.created_at.isoformat() if getattr(sb, "created_at", None) else None,
    }


class SdkOps:
    """RenderOps over the Render Python SDK (async client)."""

    def __init__(self, api_key: str, workspace_id: str):
        from render import RenderAsync

        self._render = RenderAsync(token=api_key, owner_id=workspace_id)
        self.sb = self._render.sandboxes
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
        from render.experimental.sandbox import CredentialInjection

        inj = CredentialInjection(type=injection_type, name=None, template=None)
        if exists:
            await self.sb.credentials.update(sandbox_group_id=group_id, credential_id=credential_id, domain=domain,
                                             value=value, injection=inj, paths=paths, enabled=True)
        else:
            await self.sb.credentials.create(sandbox_group_id=group_id, credential_id=credential_id, domain=domain,
                                             value=value, injection=inj, paths=paths, enabled=True)
        return await self.get_credential(group_id, credential_id) or {}

    async def create_sandbox(self, *, group_id, name, image, plan, env, timeout_seconds, idle_timeout_seconds) -> dict:
        sb = await self.sb.create(name=name, image=image, plan=plan, env=env, timeout_seconds=timeout_seconds,
                                  idle_timeout_seconds=idle_timeout_seconds, sandbox_group_id=group_id)
        return _sandbox_dict(sb)

    async def get_sandbox(self, sandbox_id: str) -> dict | None:
        from render.sandbox import SandboxNotFoundError

        try:
            return _sandbox_dict(await self.sb.from_id(sandbox_id))
        except SandboxNotFoundError:
            return None

    async def find_sandbox(self, group_id: str, name: str) -> dict | None:
        page = await self.sb.list(name=name, sandbox_group_id=group_id, status=["running", "creating", "resuming"])
        items = sorted(page.sandboxes, key=lambda s: s.created_at)
        return _sandbox_dict(items[-1]) if items else None

    async def terminate(self, sandbox_id: str) -> None:
        await self.sb.terminate(sandbox_id)

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
