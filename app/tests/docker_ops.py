"""A Docker-backed RenderOps for integration tests. Never used in production.

Each "sandbox" is a container on the test harness network:
  * image=None (Render's default image) -> a privileged plain Debian
    container with BuildKit binaries mounted, standing in for the default
    sandbox image the image build runs in;
  * image=<imported id> -> the image pulled from the local registry by
    import_image, i.e. exactly what the build pushed.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shlex

from pgmigrate.render_ops import ExecResult, OpsError


async def _run(*args: str, timeout: float = 600, check: bool = True) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise OpsError(f"timed out: {' '.join(args)[:120]}") from None
    if check and proc.returncode != 0:
        raise OpsError(f"{' '.join(shlex.quote(a) for a in args)[:200]} failed: {err.decode()[-400:]}")
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


class DockerOps:
    def __init__(self, *, network: str, default_image: str, default_mounts: list[str],
                 registry_alias: dict[str, str]):
        self.network = network
        self.default_image = default_image
        self.default_mounts = default_mounts
        self.registry_alias = registry_alias
        self.credentials: dict[tuple[str, str], dict] = {}
        self.images: dict[str, dict] = {}
        self.calls: list[tuple] = []

    @staticmethod
    def _c(sid: str) -> str:
        return f"pgm-fake-{sid}"

    async def ensure_group(self, name):
        return f"sbg-{name}"

    async def get_credential(self, group_id, credential_id):
        return self.credentials.get((group_id, credential_id))

    async def put_credential(self, group_id, credential_id, *, domain, value, paths, injection_type, exists):
        self.calls.append(("put_credential", group_id, domain, paths, injection_type))
        c = {"id": credential_id, "domain": domain, "paths": paths, "injection": injection_type, "enabled": True,
             "methods": [], "version": len(self.calls), "updated_at": None}
        self.credentials[(group_id, credential_id)] = c
        return c

    async def create_sandbox(self, *, group_id, name, image, plan, env, timeout_seconds, idle_timeout_seconds):
        sid = "sbx-" + secrets.token_hex(6)
        self.calls.append(("create_sandbox", sid, group_id, name, image, tuple(sorted(env))))
        args = ["docker", "run", "-d", "--name", self._c(sid), "--network", self.network,
                "--label", "pgm-fake=1", "--label", f"pgm.name={name}", "--label", f"pgm.group={group_id}"]
        for k, v in env.items():
            args += ["-e", f"{k}={v}"]
        if image is None:
            args += ["--privileged", *[x for m in self.default_mounts for x in ("-v", m)], "--entrypoint", "sleep",
                     self.default_image, "infinity"]
        else:
            args += ["--entrypoint", "sleep", self.images[image]["local"], "infinity"]
        await _run(*args)
        return {"id": sid, "name": name, "status": "creating", "plan": plan, "sandbox_group_id": group_id}

    async def get_sandbox(self, sandbox_id):
        rc, out, _ = await _run("docker", "inspect", self._c(sandbox_id), check=False)
        if rc != 0:
            return None
        info = json.loads(out)[0]
        status = "running" if info["State"]["Running"] else "terminated"
        labels = info["Config"]["Labels"]
        return {"id": sandbox_id, "name": labels.get("pgm.name"), "status": status, "plan": None,
                "sandbox_group_id": labels.get("pgm.group")}

    async def find_sandbox(self, group_id, name):
        _, out, _ = await _run("docker", "ps", "-q", "--filter", f"label=pgm.name={name}",
                               "--filter", f"label=pgm.group={group_id}", "--format", "{{.Names}}")
        for cname in out.split():
            return await self.get_sandbox(cname.removeprefix("pgm-fake-"))
        return None

    async def terminate(self, sandbox_id):
        self.calls.append(("terminate", sandbox_id))
        await _run("docker", "rm", "-f", self._c(sandbox_id), check=False)

    async def exec(self, sandbox_id, command, timeout=120):
        rc, out, err = await _run("docker", "exec", self._c(sandbox_id), "bash", "-c", command,
                                  timeout=timeout, check=False)
        return ExecResult(rc, out, err)

    async def copy_to(self, sandbox_id, local_path, remote_path):
        c = self._c(sandbox_id)
        if os.path.isdir(local_path):
            await _run("docker", "exec", c, "mkdir", "-p", remote_path)
            await _run("docker", "cp", "-q", f"{local_path}/.", f"{c}:{remote_path}")
        else:
            await _run("docker", "exec", c, "mkdir", "-p", os.path.dirname(remote_path))
            await _run("docker", "cp", "-q", local_path, f"{c}:{remote_path}")

    async def copy_from(self, sandbox_id, remote_path, local_path):
        await _run("docker", "cp", "-q", f"{self._c(sandbox_id)}:{remote_path}", local_path)
        return os.path.join(local_path, os.path.basename(remote_path.rstrip("/")))

    async def import_image(self, *, group_id, name, source, username, password):
        ref = source
        for a, b in self.registry_alias.items():
            ref = ref.replace(a, b, 1)
        self.calls.append(("import_image", group_id, source))
        await _run("docker", "pull", "-q", ref, timeout=900)
        iid = "sbb-" + secrets.token_hex(6)
        self.images[iid] = {"id": iid, "name": name, "status": "succeeded", "local": ref,
                            "digest": source.split("@")[-1], "failure_reason": None}
        return {**self.images[iid], "status": "running"}

    async def get_image(self, group_id, image_id):
        return self.images[image_id]

    async def snapshot(self, sandbox_id, name):
        return {"id": "snp-" + secrets.token_hex(4), "status": "available", "name": name}

    async def cleanup(self):
        _, out, _ = await _run("docker", "ps", "-aq", "--filter", "label=pgm-fake=1", check=False)
        for cid in out.split():
            await _run("docker", "rm", "-f", cid, check=False)
