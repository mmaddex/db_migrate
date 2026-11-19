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
