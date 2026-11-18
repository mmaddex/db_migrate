import asyncio
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def pg_server():
    """A Postgres for the app's own state: TEST_DATABASE_URL, or a throwaway container."""
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return
    image = os.environ.get("TEST_PG_IMAGE", "postgres:17")
    port = _free_port()
    name = f"pgm-unit-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=pw",
                    "-p", f"127.0.0.1:{port}:5432", image], check=True, capture_output=True)
    url = f"postgresql://postgres:pw@127.0.0.1:{port}/postgres"
    try:
        for _ in range(60):
            try:
                with psycopg.connect(url, connect_timeout=2) as c:
                    c.execute("SELECT 1")
                break
            except psycopg.OperationalError:
                time.sleep(0.5)
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
def database_url(pg_server):
    """A fresh database per test."""
    db = "t_" + uuid.uuid4().hex[:10]
    with psycopg.connect(pg_server, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {db}")
    base = pg_server.rsplit("/", 1)[0]
    yield f"{base}/{db}"
    with psycopg.connect(pg_server, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


async def wait_for(desc, fn, timeout=30.0, interval=0.1):
    end = time.monotonic() + timeout
    while True:
        v = await fn()
        if v:
            return v
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {desc}")
        await asyncio.sleep(interval)
