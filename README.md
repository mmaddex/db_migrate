# db_migrate: Heroku Postgres to Render Postgres with Bucardo

Moves a Heroku Postgres database to Render Postgres with near-zero
downtime. Bucardo copies every table, then keeps replicating changes until
you cut over.

Everything runs on Render; nothing runs on your laptop:

- **A web app** (this repo's `app/`, deployed with `render.yaml`) where you
  create migrations, enter the connection strings, and click through the
  runbook while watching live status. Several migrations can run at once.
- **One sandbox per migration**, started from a custom image that holds
  Bucardo, a local Postgres for Bucardo's control database, the Postgres
  client tools, and the migration scripts (`sandbox/`).
- **The image is built in a sandbox too**, from Render's default image, and
  pushed to your GitHub container registry. The build sandbox never sees
  your GitHub token: a brokered credential on its sandbox group adds the
  token to registry requests on the way out.

## Deploy

1. Create the Blueprint from this repo (`render.yaml`): a Python web service
   and a small Postgres for the app's own state.
2. In the web service's **Environment**, set:

   | Variable | Value |
   |---|---|
   | `APP_PASSWORD` | Password for the UI (HTTP basic auth, user `admin`) |
   | `RENDER_API_KEY` | API key of a **workspace admin**: creating brokered credentials needs admin |
   | `RENDER_WORKSPACE_ID` | The workspace the sandboxes belong to (`tea-...`) |
   | `GITHUB_REGISTRY` | e.g. `ghcr.io/your-org` (the image becomes `ghcr.io/your-org/pg-migrate:<commit>`) |
   | `GITHUB_USERNAME` | The GitHub user the token belongs to |
   | `GITHUB_TOKEN` | Classic PAT with `write:packages` (includes `read:packages`) |

   `APP_SECRET_KEY` is generated for you. It encrypts stored connection
   strings, so don't change it once migrations exist. `PG_MAJOR` (default
   17) must be Render's Postgres major version and at least Heroku's.
3. Open the app and go to **Image & settings > Build & push image**.
4. Select **New migration** and follow the runbook. See [docs/runbook.md](docs/runbook.md).

## How the image gets built

```
web app ──create──▶ build sandbox (Render default image, group pg-migration-build)
   │                   │ uploads image/, sandbox/, builder/ from this deploy
   │                   │ builder/build_image.sh: downloads BuildKit, builds, pushes
   │                   ▼
   │                ghcr.io ◀── GET /token gets "Authorization: Basic user:token"
   │                            added by Render's credential broker, not the sandbox
   └──import by digest──▶ sandbox group pg-migration ──▶ migration sandboxes
```

- On each build the app makes sure the build group has one brokered
  credential: id `registry-token`, domain = the registry host, path
  `/token`, injection `basic`, value `user:token`. A registry client asks
  `/token` for a short-lived bearer token before pushing. The broker
  authenticates that one request, and the push then uses the bearer token,
  which nothing overwrites. The credential is rewritten only when the token
  changes.
- Builds are tagged with the deployed commit (`RENDER_GIT_COMMIT`), so the
  image always matches the app's own `sandbox/` scripts. The build log
  starts with facts about the default image (user, arch, OS, PID 1). The
  first build therefore answers the platform questions in
  [docs/spike.md](docs/spike.md).
- The finished image is imported into the migration group by digest, using
  `GITHUB_USERNAME`/`GITHUB_TOKEN` for that one import. Render does not
  store them.

## How a migration works

1. **Preflight** checks both databases read-only. It blocks on missing
   trigger-disable permission (`SET session_replication_role`), tables
   without a primary key, missing extensions, or unreachable databases.
2. **Schema** loads the Heroku schema into Render *without* its
   `CREATE INDEX` statements; constraints stay.
3. **Start sync** configures one Bucardo relgroup and one sync with
   `onetimecopy=1`. Bucardo copies everything, then replicates changes. A
   watchdog restarts Bucardo or the local Postgres if either stops.
4. **Build indexes** creates every deferred index `CONCURRENTLY` on Render
   after the copy. Getsafe measured more than 100x faster copies this way.
5. **Cutover**, after Heroku writes stop, does five things:
   - waits for a sync that started after the stop;
   - requires exact row counts to match on every table;
   - sets sequences;
   - refreshes materialized views and runs ANALYZE;
   - stops Bucardo.
6. **Clean up Heroku** drops the `bucardo` schema and its triggers.
   **Tear down** saves the artifacts and terminates the sandbox.

Every step is a job. Scripts run detached inside the sandbox (`job.sh`);
the app tails their logs and checkpoints progress in Postgres. A deploy or
restart of the web app therefore resumes running jobs instead of losing
them. Status (phase, row counts, copy and index progress) comes from the
databases, refreshed every 30 seconds.

## Secrets

- Connection strings are encrypted in the app's database (Fernet, keyed
  from `APP_SECRET_KEY`). The browser never receives them, and job logs are
  redacted against them.
- A migration sandbox gets them as environment variables at creation. Inside
  it they go into a libpq service file outside the artifact directory, so
  passwords never appear in command lines, Bucardo's tables, script output,
  or downloadable artifacts.
- The GitHub token is used only for the brokered credential and the image
  import. Tests assert that it never appears in any sandbox command,
  environment, upload or log.
- The UI requires HTTP basic auth and accepts only its own hostname
  (`RENDER_EXTERNAL_HOSTNAME`, plus `ALLOWED_HOSTS`). State-changing requests
  need a custom header and a same-origin `Origin`.

## Layout

| Path | What |
|---|---|
| `app/` | The web app: Starlette, the Render Python SDK, Postgres for state |
| `image/` | Dockerfile for the migration image, the pinned PGDG key, `bucardorc` |
| `sandbox/` | Scripts baked into `/opt/migrate` in the image |
| `builder/` | `build_image.sh`, run in the build sandbox |
| `render.yaml` | Blueprint: web service + Postgres |
| `test/` | Docker Compose harness and shell tests |

## Testing

```bash
test/unit_lib.sh           # shell helpers: URL parsing, service file, redaction
test/e2e.sh                # the migration scripts against Postgres containers
test/build_image_test.sh   # builder/build_image.sh in a plain Debian container
cd app && pip install -r requirements-dev.txt && python -m pytest   # web app unit tests
PGM_E2E=1 python -m pytest tests/test_e2e.py                        # the web app end to end
```

`app/tests/test_e2e.py` uses a Docker-backed stand-in for the Render API. It
runs the image build in a "default image" container, pushes to a local
registry, and imports the exact digest. It then runs two migrations in
their own sandboxes: one through the whole runbook, the other blocked at
preflight. It restarts the web app mid-build to prove jobs resume. CI runs
all of these.

## What testing taught us

- **Generated columns.** Bucardo 5.6.0 puts generated columns in its COPY
  column list, which Postgres rejects. `start_sync.sh` adds a `customcols`
  clause listing only the ordinary columns.
- **`bucardo kick <sync> 0`** can return on a run that started before writes
  stopped, so cutover kicks twice.
- **BuildKit** finds its OCI runtime only as `runc` on `PATH`; its
  `--oci-worker-binary` flag doesn't satisfy that check.
- **Reruns.** `job.sh` used to append each run's output to the same log, so
  a rerun of preflight re-read the previous run's blockers. Each run now
  starts a fresh log.
- **Zombies** from a PID 1 that never reaps still answer `kill -0`; process
  checks treat them as dead.

## Known limits

- Sandboxes, image import and credential brokering are in beta or alpha.
  Someone should watch each migration.
- Not yet run against real Render: the tests stand in for the Render API
  with Docker. The first real build and the Diagnostics action answer the
  open platform questions in [docs/spike.md](docs/spike.md).
- One-way: writes made to Render after cutover don't flow back to Heroku.
- Indexes are built under Render's collation; if preflight reports a
  difference, a person decides whether the sort-order change is acceptable.
- Large objects are not migrated.
