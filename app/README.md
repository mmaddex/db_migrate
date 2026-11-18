# pg-migrate web app

Starlette app that drives migrations through the Render Python SDK
(`render`), with its own state in Postgres. Deployed by `render.yaml`;
see the repository README for settings.

| Module | What |
|---|---|
| `pgmigrate/web.py` | HTTP API, auth and request guards, static UI |
| `pgmigrate/runbook.py` | Actions as resumable jobs; image build with credential brokering |
| `pgmigrate/worker.py` | Runs jobs and refreshes status (one leader via an advisory lock) |
| `pgmigrate/render_ops.py` | Everything asked of Render, over the SDK |
| `pgmigrate/store.py` | Postgres state: migrations, jobs and logs, images, artifacts |
| `pgmigrate/vault.py` | Encryption of stored connection strings, and redaction |
| `pgmigrate/static/` | The UI (no build step) |

The app has no login of its own: on Render, the service's authentication
(`render.yaml`) decides who gets in. It refuses to start outside Render
(`RENDER=true`) unless `PGM_DEV=1`. Keep dev mode on 127.0.0.1.

Run it locally against any Postgres:

```bash
pip install -r requirements-dev.txt
```

```bash
PGM_DEV=1 DATABASE_URL=postgresql://... APP_SECRET_KEY=dev \
  uvicorn --factory pgmigrate.web:app_from_env --host 127.0.0.1 --port 8000
```

Without `RENDER_API_KEY`/`RENDER_WORKSPACE_ID` the UI works, and every step
fails with a message naming the missing settings.

Tests:

```bash
python -m pytest
```

```bash
PGM_E2E=1 python -m pytest tests/test_e2e.py
```

The unit tests start a throwaway Postgres in Docker unless
`TEST_DATABASE_URL` is set. The e2e test needs Docker and runs everything
against `test/docker-compose.yml`; `PGM_SHOTS=<dir>` saves screenshots
(needs node with Playwright).

Brand fonts: the UI uses PP Neue Montreal when `app/fonts/` holds
`PPNeueMontreal-Variable.woff2` and `PPNeueMontrealMono-Regular.woff2`
(licensed for internal use, so gitignored), and falls back to Manrope.
