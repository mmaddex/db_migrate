"""SdkOps error handling around the alpha credential brokering API."""

import pytest
from render.client.errors import ClientError

import pgmigrate.render_ops as ro
from pgmigrate.render_ops import OpsError, SdkOps


class FakeCredentials:
    def __init__(self, statuses):
        self.statuses = list(statuses)  # one HTTP status per call; 201 means saved
        self.calls = []

    async def _call(self, kind, **kw):
        self.calls.append(kind)
        status = self.statuses.pop(0)
        if status != 201:
            raise ClientError("create credential failed: not found", status_code=status)

    async def create(self, **kw):
        await self._call("create", **kw)

    async def update(self, **kw):
        await self._call("update", **kw)


def ops_with(statuses) -> SdkOps:
    ops = SdkOps.__new__(SdkOps)  # no SDK client: only .sb.credentials is used
    ops.workspace_id = "tea-test"
    ops.sb = type("SB", (), {"credentials": FakeCredentials(statuses)})()

    async def get_credential(group_id, credential_id):
        return {"id": credential_id}
    ops.get_credential = get_credential
    return ops


async def put(ops, exists=False):
    return await ops.put_credential("sbg-1", "registry-token", domain="ghcr.io", value="u:t", paths=["/token"],
                                    injection_type="basic", exists=exists)


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(ro, "CREDENTIAL_RETRY_SECONDS", 0.0)


async def test_saves_after_a_404_from_a_group_created_a_moment_ago():
    ops = ops_with([404, 201])
    assert await put(ops) == {"id": "registry-token"}
    assert ops.sb.credentials.calls == ["create", "create"]


async def test_404_that_persists_says_brokering_is_probably_off_for_the_workspace():
    ops = ops_with([404, 404, 404])
    with pytest.raises(OpsError, match="not enabled for workspace tea-test") as e:
        await put(ops)
    assert "sbg-1" in str(e.value) and "u:t" not in str(e.value)
    assert ops.sb.credentials.calls == ["create"] * ro.CREDENTIAL_ATTEMPTS


async def test_403_asks_for_an_admin_key_without_retrying():
    ops = ops_with([403])
    with pytest.raises(OpsError, match="workspace admin"):
        await put(ops, exists=True)
    assert ops.sb.credentials.calls == ["update"]


async def test_other_errors_pass_through():
    ops = ops_with([500])
    with pytest.raises(ClientError):
        await put(ops)


# ------------------------------------------------------------ network policy
@pytest.mark.parametrize("value,want", [
    ("allow-all", ("allow-all", ())),
    (" Deny-All ", ("deny-all", ())),
    ("github.com, *.githubusercontent.com,github.com", ("allow-list", ("github.com", "*.githubusercontent.com"))),
])
def test_parse_network_policy(value, want):
    assert ro.parse_network_policy(value) == want


@pytest.mark.parametrize("value", ["", " , ", "https://github.com", "github.com:443", "github.com/x",
                                   "api.*.github.com", "*", "allow_all"])
def test_parse_network_policy_rejects(value):
    with pytest.raises(ValueError):
        ro.parse_network_policy(value, "BUILD_NETWORK_POLICY")


def test_sdk_network_policy():
    p = ro.sdk_network_policy("allow-all")
    assert p.type == "allow-all" and p.rules == []
    p = ro.sdk_network_policy("github.com,*.githubusercontent.com")
    assert p.type == "allow-list" and [(r.domain, r.protocol) for r in p.rules] == [
        ("github.com", "https"), ("*.githubusercontent.com", "https")]


async def test_create_sandbox_sends_the_network_policy():
    ops = SdkOps.__new__(SdkOps)
    seen = {}

    class SB:
        async def create(self, **kw):
            seen.update(kw)
            return type("S", (), {"id": "sbx-1", "status": "running", "created_at": None})()
    ops.sb = SB()
    await ops.create_sandbox(group_id="sbg-1", name="n", image=None, plan="4c-8g", env={}, timeout_seconds=0,
                             idle_timeout_seconds=0, network_policy="deny-all")
    assert seen["network_policy"].type == "deny-all" and seen["sandbox_group_id"] == "sbg-1"


def test_config_reads_and_validates_network_policies(monkeypatch):
    from pgmigrate.config import Config
    monkeypatch.setenv("BUILD_NETWORK_POLICY", "github.com")
    monkeypatch.delenv("SANDBOX_NETWORK_POLICY", raising=False)
    cfg = Config.from_env()
    assert cfg.build_network_policy == "github.com" and cfg.sandbox_network_policy == "allow-all"
    monkeypatch.setenv("SANDBOX_NETWORK_POLICY", "https://github.com")
    with pytest.raises(ValueError, match="SANDBOX_NETWORK_POLICY"):
        Config.from_env()
