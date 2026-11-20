"""In-memory RenderOps for unit tests. Records every call so tests can assert
on what reached a sandbox (for example, that the registry token never did)."""

from __future__ import annotations

import itertools
import os
import shutil

from pgmigrate.render_ops import ALIVE, ExecResult

_ids = itertools.count(1)


class FakeOps:
    def __init__(self):
        self.calls: list[tuple] = []
        self.groups: dict[str, str] = {}
        self.credentials: dict[tuple[str, str], dict] = {}
        self.credential_values: dict[tuple[str, str], str] = {}
        self.sandboxes: dict[str, dict] = {}
        self.images: dict[str, dict] = {}
        self.uploads: dict[str, list[str]] = {}
        self.exec_handler = lambda sid, cmd: ExecResult(0, "", "")

    def suspend(self, sandbox_id):
        """What Render does after the sandbox's idle timeout."""
        self.sandboxes[sandbox_id]["status"] = "suspended"

    async def ensure_group(self, name):
        self.calls.append(("ensure_group", name))
        return self.groups.setdefault(name, f"sbg-{name}")

    async def get_credential(self, group_id, credential_id):
        return self.credentials.get((group_id, credential_id))

    async def put_credential(self, group_id, credential_id, *, domain, value, paths, injection_type, exists):
        self.calls.append(("put_credential", group_id, credential_id, domain, paths, injection_type, exists))
        cur = self.credentials.get((group_id, credential_id))
        version = (cur["version"] + 1) if cur else 1
        c = {"id": credential_id, "domain": domain, "paths": paths, "injection": injection_type,
             "enabled": True, "methods": [], "version": version, "updated_at": None}
        self.credentials[(group_id, credential_id)] = c
        self.credential_values[(group_id, credential_id)] = value
        return c

    async def create_sandbox(self, *, group_id, name, image, plan, env, timeout_seconds, idle_timeout_seconds,
                             network_policy="allow-all"):
        sid = f"sbx-fake{next(_ids):05d}"
        sb = {"id": sid, "name": name, "status": "running", "plan": plan, "sandbox_group_id": group_id,
              "image": image, "env": dict(env), "timeout": timeout_seconds, "idle": idle_timeout_seconds,
              "network_policy": network_policy}
        self.sandboxes[sid] = sb
        self.calls.append(("create_sandbox", sid, group_id, name, image, plan, tuple(sorted(env))))
        return await self.get_sandbox(sid)

    async def get_sandbox(self, sandbox_id):
        sb = self.sandboxes.get(sandbox_id)
        if not sb:
            return None
        return {**{k: sb[k] for k in ("id", "name", "status", "plan", "sandbox_group_id")},
                "idle_timeout_seconds": sb["idle"], "network_policy": sb["network_policy"]}

    async def find_sandbox(self, group_id, name):
        for sb in self.sandboxes.values():
            if sb["name"] == name and sb["sandbox_group_id"] == group_id and sb["status"] in ALIVE:
                return await self.get_sandbox(sb["id"])
        return None

    async def terminate(self, sandbox_id):
        self.calls.append(("terminate", sandbox_id))
        self.sandboxes[sandbox_id]["status"] = "terminated"

    async def wake(self, sandbox_id):
        self.calls.append(("wake", sandbox_id))
        await self.exec(sandbox_id, "true")

    async def exec(self, sandbox_id, command, timeout=120):
        # Like Render: a command resumes a suspended sandbox.
        sb = self.sandboxes.get(sandbox_id)
        if sb and sb["status"] == "suspended":
            sb["status"] = "running"
            self.calls.append(("resumed", sandbox_id))
        self.calls.append(("exec", sandbox_id, command))
        return self.exec_handler(sandbox_id, command)

    async def copy_to(self, sandbox_id, local_path, remote_path):
        files = []
        if os.path.isdir(local_path):
            for root, _, names in os.walk(local_path):
                files += [os.path.relpath(os.path.join(root, n), local_path) for n in names]
            contents = None
        else:
            with open(local_path) as f:
                contents = f.read()
        self.calls.append(("copy_to", sandbox_id, remote_path, contents))
        self.uploads[sandbox_id] = files
        return None

    async def copy_from(self, sandbox_id, remote_path, local_path):
        self.calls.append(("copy_from", sandbox_id, remote_path))
        d = os.path.join(local_path, "migrate")
        os.makedirs(os.path.join(d, "artifacts"), exist_ok=True)
        with open(os.path.join(d, "artifacts", "preflight.txt"), "w") as f:
            f.write("RESULT: pass\n")
        return d

    async def import_image(self, *, group_id, name, source, username, password):
        iid = f"sbb-fake{next(_ids):05d}"
        self.images[iid] = {"id": iid, "name": name, "status": "succeeded", "digest": source.split("@")[-1],
                            "failure_reason": None, "source": source}
        self.calls.append(("import_image", group_id, name, source, username, bool(password)))
        return dict(self.images[iid], status="running")

    async def get_image(self, group_id, image_id):
        return self.images[image_id]

    async def snapshot(self, sandbox_id, name):
        self.calls.append(("snapshot", sandbox_id, name))
        return {"id": "snp-fake", "status": "available", "name": name}


def remote_script_handler(outputs: dict[str, tuple[str, int]]):
    """exec handler simulating /opt/migrate/job.sh for scripts that finish at once:
    outputs maps script -> (log text, exit code)."""
    state: dict[str, dict] = {}

    def handle(sid, cmd):
        parts = cmd.split()
        if cmd.startswith("/opt/migrate/job.sh "):
            verb, script = parts[1], parts[2]
            text, rc = outputs.get(script, ("", 0))
            if verb == "start":
                state[script] = {"text": text, "rc": rc}
                return ExecResult(0, "started\n", "")
            if verb == "status":
                s = state.get(script)
                if not s:
                    return ExecResult(0, "not started\n", "")
                return ExecResult(0, "succeeded\n" if s["rc"] == 0 else f"failed (rc {s['rc']})\n", "")
            if verb == "log":
                off = int(parts[3])
                data = state.get(script, {"text": ""})["text"].encode()
                return ExecResult(0, f"SIZE={len(data)}\n" + data[off:].decode(), "")
            if verb == "stop":
                return ExecResult(0, "stopping\n", "")
        if cmd.startswith("/opt/migrate/status.sh"):
            return ExecResult(0, '{"phase": "schema-loaded", "markers": {}, "tables": [], "totals": {}}', "")
        return ExecResult(0, "", "")

    return handle


def cleanup_dir(path):
    shutil.rmtree(path, ignore_errors=True)
