"""Background work inside the web process: run jobs, refresh status.

Only one process does this at a time (a Postgres advisory lock), so the web
service can run more than one instance without two workers driving the
same sandbox.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import psycopg

from .config import Config
from .render_ops import RenderOps
from .runbook import Engine, status_report
from .store import Store

log = logging.getLogger("pgmigrate.worker")
LOCK_KEY = 0x70676D69  # "pgmi"


class Worker:
    def __init__(self, cfg: Config, store: Store, ops: RenderOps, engine: Engine):
        self.cfg, self.store, self.ops, self.engine = cfg, store, ops, engine
        self.engine.on_migration_changed = self.request_refresh
        self.running: dict[str, asyncio.Task] = {}
        self.refreshing: set[str] = set()
        self.is_leader = False
        self._tasks: list[asyncio.Task] = []
        self._lock_conn: psycopg.AsyncConnection | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._lead(), name="pgm-leader")]

    async def stop(self) -> None:
        self._stop.set()
        for t in [*self._tasks, *self.running.values()]:
            t.cancel()
        await asyncio.gather(*self._tasks, *self.running.values(), return_exceptions=True)
        if self._lock_conn is not None:
            await self._lock_conn.close()

    async def _lead(self) -> None:
        while not self._stop.is_set():
            try:
                if self._lock_conn is None or self._lock_conn.closed:
                    self._lock_conn = await psycopg.AsyncConnection.connect(self.cfg.database_url, autocommit=True)
                cur = await self._lock_conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,))
                self.is_leader = (await cur.fetchone())[0]
            except Exception:  # noqa: BLE001
                log.exception("leader election failed")
                self.is_leader = False
            if self.is_leader:
                log.info("this instance runs jobs and status checks")
                await asyncio.gather(self._jobs_loop(), self._status_loop())
                return
            await asyncio.sleep(15)

    # ------------------------------------------------------------- jobs
    async def _jobs_loop(self) -> None:
        while not self._stop.is_set():
            try:
                for job in await self.store.runnable_jobs():
                    if job["id"] not in self.running:
                        self.running[job["id"]] = asyncio.create_task(self._run(job), name=f"job-{job['id']}")
            except Exception:  # noqa: BLE001
                log.exception("job loop")
            await asyncio.sleep(1)

    async def _run(self, job: dict) -> None:
        try:
            await self.engine.run(job)
        finally:
            self.running.pop(job["id"], None)

    # ----------------------------------------------------------- status
    async def _status_loop(self) -> None:
        while not self._stop.is_set():
            try:
                for m in await self.store.list_migrations():
                    if m.get("sandbox_id") and not m.get("torn_down_at"):
                        self.request_refresh(m["id"])
            except Exception:  # noqa: BLE001
                log.exception("status loop")
            await asyncio.sleep(self.cfg.poll_seconds)

    def request_refresh(self, migration_id: str) -> None:
        if migration_id in self.refreshing:
            return
        self.refreshing.add(migration_id)
        asyncio.create_task(self._refresh(migration_id))

    async def _refresh(self, migration_id: str) -> None:
        try:
            m = await self.store.get_migration(migration_id)
            if not m or not m.get("sandbox_id"):
                return
            try:
                report, err = await status_report(self.cfg, self.ops, m)
            except Exception as e:  # noqa: BLE001 - keep the last report, show the error
                await self.store.update_migration(migration_id, status_error=f"status check failed: {e}",
                                                  status_at=datetime.now(timezone.utc))
                return
            if report.get("status") is None and m.get("status") and m["status"].get("status"):
                # Keep the last known in-sandbox status when this check couldn't get one.
                report["status"] = m["status"]["status"]
            await self.store.update_migration(migration_id, status=report, status_error=err,
                                              status_at=datetime.now(timezone.utc))
        finally:
            self.refreshing.discard(migration_id)
