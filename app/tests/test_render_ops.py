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


def test_config_reads_and_validates_the_build_network_policy(monkeypatch):
    from pgmigrate.config import Config
    monkeypatch.setenv("BUILD_NETWORK_POLICY", "github.com")
    assert Config.from_env().build_network_policy == "github.com"
    monkeypatch.setenv("BUILD_NETWORK_POLICY", "https://github.com")
    with pytest.raises(ValueError, match="BUILD_NETWORK_POLICY"):
        Config.from_env()


def test_migration_network_policy_is_built_from_the_migrations_urls():
    from pgmigrate.config import migration_network_policy
    heroku = "postgres://u:p@EC2-1-2-3-4.compute-1.amazonaws.com:5432/d"
    render = "postgresql://r:q@dpg-abc-a.oregon-postgres.render.com/app"
    assert migration_network_policy(heroku, render) == \
        "ec2-1-2-3-4.compute-1.amazonaws.com,dpg-abc-a.oregon-postgres.render.com,api.ipify.org"
    assert migration_network_policy(heroku, render, "https://hooks.slack.com/services/T/B/x").split(",") == [
        "ec2-1-2-3-4.compute-1.amazonaws.com", "dpg-abc-a.oregon-postgres.render.com", "hooks.slack.com",
        "api.ipify.org"]
    with pytest.raises(ValueError, match="10.0.0.5"):
        migration_network_policy("postgres://u:p@10.0.0.5/d", render)


def test_build_allow_list_default_matches_the_blueprint():
    import re
    from pathlib import Path

    from pgmigrate.config import DEFAULT_BUILD_NETWORK_POLICY, Config
    kind, hosts = ro.parse_network_policy(DEFAULT_BUILD_NETWORK_POLICY)
    assert kind == "allow-list" and set(hosts) == {
        "github.com", "release-assets.githubusercontent.com", "mirror.gcr.io",
        "deb.debian.org", "apt.postgresql.org", "ghcr.io"}
    assert Config().build_network_policy == DEFAULT_BUILD_NETWORK_POLICY
    blueprint = (Path(__file__).resolve().parents[2] / "render.yaml").read_text()
    assert re.search(r"key: BUILD_NETWORK_POLICY\s+value: (\S+)", blueprint).group(1) == DEFAULT_BUILD_NETWORK_POLICY
    assert "SANDBOX_NETWORK_POLICY" not in blueprint


# ------------------------------------------------------- Postgres versions
def test_config_reads_and_validates_pg_client_majors(monkeypatch):
    from pgmigrate.config import Config
    monkeypatch.delenv("PG_CLIENT_MAJORS", raising=False)
    assert Config.from_env().pg_client_majors == "14 15 16 17 18"
    monkeypatch.setenv("PG_CLIENT_MAJORS", " 18,14  16 14 ")
    assert Config.from_env().pg_client_majors == "14 16 18"
    for bad in ("", " , ", "17.2", "pg17", "17;id"):
        monkeypatch.setenv("PG_CLIENT_MAJORS", bad)
        with pytest.raises(ValueError, match="PG_CLIENT_MAJORS"):
            Config.from_env()


def test_pg_client_majors_default_matches_the_dockerfile():
    import re
    from pathlib import Path

    from pgmigrate.config import DEFAULT_PG_CLIENT_MAJORS
    dockerfile = (Path(__file__).resolve().parents[2] / "image" / "Dockerfile").read_text()
    assert re.search(r'^ARG PG_CLIENT_MAJORS="([^"]*)"$', dockerfile, re.M).group(1) == DEFAULT_PG_CLIENT_MAJORS


# ------------------------------------------------------ suspending sandboxes
def test_config_reads_and_validates_the_sandbox_idle_timeout(monkeypatch):
    from pgmigrate.config import Config
    monkeypatch.delenv("SANDBOX_IDLE_TIMEOUT_SECONDS", raising=False)
    assert Config.from_env().sandbox_idle_timeout == 300 and Config().sandbox_idle_timeout == 300
    for value, want in (("0", 0), ("300", 300), (" 3600 ", 3600), ("86400", 86400)):
        monkeypatch.setenv("SANDBOX_IDLE_TIMEOUT_SECONDS", value)
        assert Config.from_env().sandbox_idle_timeout == want
    for bad in ("", "299", "86401", "5m", "-1", "300.5"):
        monkeypatch.setenv("SANDBOX_IDLE_TIMEOUT_SECONDS", bad)
        with pytest.raises(ValueError, match="SANDBOX_IDLE_TIMEOUT_SECONDS"):
            Config.from_env()


async def test_find_sandbox_includes_suspended_ones_and_wake_runs_a_command():
    ops = SdkOps.__new__(SdkOps)
    seen = {}

    class SB:
        async def list(self, **kw):
            seen.update(kw)
            return type("P", (), {"sandboxes": []})()

        async def exec(self, sandbox_id, command):
            seen["exec"] = (sandbox_id, command)
            for _ in ():
                yield
    ops.sb = SB()
    assert await ops.find_sandbox("sbg-1", "pg-migrate-acme") is None
    assert {"suspending", "suspended", "resuming", "running", "creating"} <= set(seen["status"])
    await ops.wake("sbx-1")
    assert seen["exec"] == ("sbx-1", "true")
