"""The web app's own state, in Postgres (DATABASE_URL).

Connection strings are stored encrypted (vault.SecretBox). Everything the
sandboxes do is recorded as jobs with their logs, so the app can restart
(a deploy) and pick up where it left off.
"""

from __future__ import annotations

import secrets as pysecrets
from datetime import datetime, timezone
from typing import Any

from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

SCHEMA = """
CREATE TABLE IF NOT EXISTS migrations (
  id                 text PRIMARY KEY,
  title              text NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  secrets            bytea NOT NULL,
  sync_name          text NOT NULL,
  schemas            text NOT NULL,
  plan               text NOT NULL,
  source             jsonb NOT NULL DEFAULT '{}',
  target             jsonb NOT NULL DEFAULT '{}',
  has_slack          boolean NOT NULL DEFAULT false,
  sandbox_id         text,
  sandbox_info       jsonb,
  torn_down_at       timestamptz,
  cleanup_done_at    timestamptz,
  preflight          jsonb,
  status             jsonb,
  status_at          timestamptz,
  status_error       text
);
CREATE TABLE IF NOT EXISTS jobs (
  id            text PRIMARY KEY,
  migration_id  text REFERENCES migrations(id) ON DELETE CASCADE,
  action        text NOT NULL,
  label         text NOT NULL,
  state         text NOT NULL,
  read_only     boolean NOT NULL DEFAULT false,
  exit_code     int,
  started_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz,
  log           text NOT NULL DEFAULT '',
  data          jsonb NOT NULL DEFAULT '{}'
);
-- One state-changing job at a time per migration ('' = the global image jobs).
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active
  ON jobs ((coalesce(migration_id, ''))) WHERE state IN ('queued', 'running') AND NOT read_only;
CREATE INDEX IF NOT EXISTS jobs_by_migration ON jobs (migration_id, started_at DESC);
CREATE TABLE IF NOT EXISTS images (
  tag          text PRIMARY KEY,
  ref          text NOT NULL,
  git_sha      text,
  digest       text,
  image_id     text,
  state        text NOT NULL,
  detail       text,
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS artifacts (
  id            bigserial PRIMARY KEY,
  migration_id  text NOT NULL REFERENCES migrations(id) ON DELETE CASCADE,
  created_at    timestamptz NOT NULL DEFAULT now(),
  name          text NOT NULL,
  size          bigint NOT NULL,
  content       bytea NOT NULL
);
ALTER TABLE migrations ADD COLUMN IF NOT EXISTS created_by text;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS started_by text;
CREATE TABLE IF NOT EXISTS kv (
  key    text PRIMARY KEY,
  value  jsonb NOT NULL
);
"""

ACTIVE = ("queued", "running")
MAX_LOG = 2_000_000


class Busy(Exception):
    """A state-changing job is already queued or running for this migration."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_job_id() -> str:
    return now().strftime("%Y%m%dT%H%M%SZ") + "-" + pysecrets.token_hex(3)


class Store:
    def __init__(self, dsn: str):
        self.pool = AsyncConnectionPool(dsn, min_size=1, max_size=8, open=False,
                                        kwargs={"row_factory": dict_row, "autocommit": True})

    async def open(self) -> None:
        await self.pool.open(wait=True, timeout=30)
        async with self.pool.connection() as c:
            await c.execute(SCHEMA)

    async def close(self) -> None:
        await self.pool.close()

    async def _one(self, sql: str, *args: Any) -> dict | None:
        async with self.pool.connection() as c:
            cur = await c.execute(sql, args)
            return await cur.fetchone()

    async def _all(self, sql: str, *args: Any) -> list[dict]:
        async with self.pool.connection() as c:
            cur = await c.execute(sql, args)
            return await cur.fetchall()

    async def _exec(self, sql: str, *args: Any) -> None:
        async with self.pool.connection() as c:
            await c.execute(sql, args)

    # ------------------------------------------------------------ migrations
    async def create_migration(self, *, id: str, title: str, secrets: bytes, sync_name: str, schemas: str,
                               plan: str, source: dict, target: dict, has_slack: bool,
                               created_by: str | None = None) -> None:
        try:
            await self._exec(
                """INSERT INTO migrations (id, title, secrets, sync_name, schemas, plan, source, target, has_slack, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                id, title, secrets, sync_name, schemas, plan, Jsonb(source), Jsonb(target), has_slack, created_by)
        except errors.UniqueViolation as e:
            raise ValueError("a migration with this name already exists") from e

    async def get_migration(self, id: str) -> dict | None:
        return await self._one("SELECT * FROM migrations WHERE id = %s", id)

    async def list_migrations(self) -> list[dict]:
        return await self._all("SELECT * FROM migrations ORDER BY created_at")

    async def update_migration(self, id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = %s" for k in fields)
        vals = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
        await self._exec(f"UPDATE migrations SET {cols} WHERE id = %s", *vals, id)

    async def delete_migration(self, id: str) -> None:
        await self._exec("DELETE FROM migrations WHERE id = %s", id)

    # ------------------------------------------------------------------ jobs
    async def create_job(self, *, migration_id: str | None, action: str, label: str,
                         read_only: bool, data: dict | None = None, started_by: str | None = None) -> dict:
        jid = new_job_id()
        try:
            return await self._one(
                """INSERT INTO jobs (id, migration_id, action, label, state, read_only, data, started_by)
                   VALUES (%s, %s, %s, %s, 'queued', %s, %s, %s) RETURNING *""",
                jid, migration_id, action, label, read_only, Jsonb(data or {}), started_by)
        except errors.UniqueViolation as e:
            active = await self.active_job(migration_id)
            raise Busy(f"{active['label'] if active else 'another step'} is still running") from e

    async def get_job(self, id: str) -> dict | None:
        return await self._one("SELECT * FROM jobs WHERE id = %s", id)

    async def list_jobs(self, migration_id: str | None, limit: int = 25) -> list[dict]:
        return await self._all(
            """SELECT id, migration_id, action, label, state, read_only, exit_code, started_at, finished_at, started_by
               FROM jobs WHERE migration_id IS NOT DISTINCT FROM %s ORDER BY started_at DESC LIMIT %s""",
            migration_id, limit)

    async def has_job(self, migration_id: str, action: str) -> bool:
        """Whether a job for this action was ever queued for the migration."""
        return await self._one("SELECT 1 AS x FROM jobs WHERE migration_id = %s AND action = %s LIMIT 1",
                               migration_id, action) is not None

    async def active_job(self, migration_id: str | None) -> dict | None:
        return await self._one(
            """SELECT id, migration_id, action, label, state, read_only, started_at, started_by FROM jobs
               WHERE migration_id IS NOT DISTINCT FROM %s AND state IN ('queued', 'running') AND NOT read_only
               LIMIT 1""", migration_id)

    async def runnable_jobs(self) -> list[dict]:
        return await self._all("SELECT * FROM jobs WHERE state IN ('queued', 'running') ORDER BY started_at")

    async def set_job(self, id: str, **fields: Any) -> None:
        cols = ", ".join(f"{k} = %s" for k in fields)
        vals = [Jsonb(v) if isinstance(v, dict) else v for v in fields.values()]
        await self._exec(f"UPDATE jobs SET {cols} WHERE id = %s", *vals, id)

    async def finish_job(self, id: str, state: str, exit_code: int | None) -> None:
        await self._exec("UPDATE jobs SET state = %s, exit_code = %s, finished_at = now() WHERE id = %s",
                         state, exit_code, id)

    async def append_log(self, id: str, text: str) -> None:
        if not text:
            return
        await self._exec(
            """UPDATE jobs SET log = CASE WHEN length(log) + length(%s) > %s
                 THEN '[earlier output trimmed]' || chr(10) || right(log || %s, %s) ELSE log || %s END
               WHERE id = %s""", text, MAX_LOG, text, MAX_LOG // 2, text, id)

    async def read_log(self, id: str, offset: int) -> tuple[str, int] | None:
        row = await self._one("SELECT substr(log, %s + 1) AS text, length(log) AS n FROM jobs WHERE id = %s",
                              max(offset, 0), id)
        if row is None:
            return None
        if offset > row["n"]:  # log was trimmed; start over
            return await self.read_log(id, 0)
        return row["text"], row["n"]

    # ---------------------------------------------------------------- images
    async def upsert_image(self, tag: str, **fields: Any) -> None:
        cols = ["tag", *fields]
        vals = [tag, *fields.values()]
        updates = ", ".join(f"{k} = EXCLUDED.{k}" for k in fields) + ", updated_at = now()"
        await self._exec(
            f"INSERT INTO images ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
            f"ON CONFLICT (tag) DO UPDATE SET {updates}", *vals)

    async def get_image(self, tag: str) -> dict | None:
        return await self._one("SELECT * FROM images WHERE tag = %s", tag)

    async def list_images(self) -> list[dict]:
        return await self._all("SELECT * FROM images ORDER BY updated_at DESC LIMIT 20")

    async def latest_ready_image(self) -> dict | None:
        return await self._one(
            "SELECT * FROM images WHERE state = 'ready' AND image_id IS NOT NULL ORDER BY updated_at DESC LIMIT 1")

    # ------------------------------------------------------------- artifacts
    async def add_artifact(self, migration_id: str, name: str, content: bytes) -> int:
        row = await self._one(
            "INSERT INTO artifacts (migration_id, name, size, content) VALUES (%s, %s, %s, %s) RETURNING id",
            migration_id, name, len(content), content)
        return row["id"]

    async def list_artifacts(self, migration_id: str) -> list[dict]:
        return await self._all(
            "SELECT id, name, size, created_at FROM artifacts WHERE migration_id = %s ORDER BY created_at DESC",
            migration_id)

    async def get_artifact(self, migration_id: str, aid: int) -> dict | None:
        return await self._one("SELECT * FROM artifacts WHERE migration_id = %s AND id = %s", migration_id, aid)

    # -------------------------------------------------------------------- kv
    async def get_kv(self, key: str) -> Any:
        row = await self._one("SELECT value FROM kv WHERE key = %s", key)
        return row["value"] if row else None

    async def set_kv(self, key: str, value: Any) -> None:
        await self._exec("INSERT INTO kv (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         key, Jsonb(value))


def jsonable(row: dict | None) -> dict | None:
    """Rows to JSON-safe dicts (datetimes to ISO strings, bytes dropped)."""
    if row is None:
        return None
    out = {}
    for k, v in row.items():
        if isinstance(v, (bytes, memoryview)):
            continue
        out[k] = v.isoformat() if isinstance(v, datetime) else v
    return out


__all__ = ["Store", "Busy", "jsonable", "ACTIVE"]
