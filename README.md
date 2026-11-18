# db_migrate: Heroku Postgres to Render Postgres with Bucardo

Moves a Heroku Postgres database to Render Postgres with near-zero
downtime. Bucardo copies every table, then keeps replicating changes until
you cut over. Each migration runs in its own Render sandbox, started from a
custom image that holds Bucardo, a local Postgres for Bucardo's control
database, the Postgres client tools and the migration scripts.

You can drive a migration from the command line (`local/` scripts) or from
a local web UI that runs the same scripts and shows live status. Several
migrations can run at once, each with its own sandbox.

## Requirements

- Render CLI **2.36.0 or later**, logged in (`render login`, or
  `RENDER_API_KEY`) with the right workspace selected. 2.36.0 is the
  minimum because `sandboxes create --name` arrived there.
- Docker with buildx, to build the image, plus a registry you can push to.
- `jq`, `perl`, and bash (macOS's bash 3.2 works).
- Go 1.22+ to run the web UI.
- On the Render database, the migration role must be able to
  `SET session_replication_role = replica`. On PG 15+ an admin can run
  `GRANT SET ON PARAMETER session_replication_role TO <role>`. Preflight
  blocks the migration without it.

## Quick start

Clone the repository where you keep it (for example
`/Users/mmaddex/Documents/db_migrate`), then:

```bash
cp local/config.local.sh.example local/config.local.sh   # set IMAGE_REPO, PG_MAJOR
```

### With the web UI

```bash
cd webui && go run . -repo ..
```

Open http://127.0.0.1:7788. Under **Image & environment**, build and import
the image. Then select **New migration**, enter the Heroku URL and Render's
*external* URL, and follow the runbook on the migration's page. Each step's
button runs one script and streams its output. See [webui/README.md](webui/README.md).

### From the command line

```bash
local/build_image.sh
local/import_image.sh
mkdir -p migrations/acme && cp migration.env.example migrations/acme/migration.env   # edit it
MIGRATION=acme local/create_sandbox.sh
MIGRATION=acme local/run.sh preflight.sh
```

Then follow [docs/runbook.md](docs/runbook.md).

## How it works

1. **Preflight** checks both databases read-only and blocks on missing
   trigger-disable permission, tables without a primary key, missing
   extensions, or unreachable databases.
2. **Schema** dumps the Heroku schema, loads it into Render *without* its
   `CREATE INDEX` statements, and keeps constraints (PK, unique, FK).
3. **Start sync** configures one Bucardo relgroup and one sync with
   `onetimecopy=1`. Bucardo copies every table, then replicates changes. A
   watchdog restarts Bucardo or the local Postgres if either stops.
4. **Build indexes** runs each deferred index as `CREATE INDEX CONCURRENTLY`
   on Render once the copy is done. Dropping indexes before the copy made
   it more than 100x faster for Getsafe, and every index is built under
   Render's collation.
5. **Cutover**, after Heroku writes stop: waits for a sync run that started
   after the stop, requires exact row counts to match, sets sequences,
   refreshes materialized views, runs ANALYZE, and stops Bucardo.
6. **Cleanup** drops the `bucardo` schema (and with it Bucardo's triggers)
   from Heroku, then you tear down the sandbox.

Status (phase, row counts, copy and index progress) comes from the
databases, not from Bucardo's own output.

## Layout

| Path | What |
|---|---|
| `image/` | Dockerfile for the sandbox image, the pinned PGDG key, and `bucardorc` |
| `sandbox/` | Scripts baked into `/opt/migrate` in the image |
| `local/` | Operator scripts: build, import, create, run, status, fetch, hotfix, snapshot, teardown |
| `webui/` | The web UI (Go, standard library only) |
| `spike/` | Phase 0 platform spike; results go in [docs/spike.md](docs/spike.md) |
| `test/` | Docker Compose harness, end-to-end tests, a fake `render` CLI for tests |
| `migrations/<name>/` | Per-migration state, gitignored: `migration.env` (secrets, 0600), `migration.conf`, `.sandbox_id`, `artifacts/`, job logs |

## Secrets

- Connection strings live only in `migrations/<name>/migration.env` (mode
  600) on your machine. They reach the sandbox through `--env-file` at
  creation, never through the image.
- Inside the sandbox, the scripts write them to a libpq service file outside
  the work directory. Passwords never appear in command lines, Bucardo's
  tables, script output, or downloaded artifacts (`test/e2e.sh` checks the
  artifacts).
- The web UI never sends connection strings back to the browser and redacts
  job logs as a second line of defence.

## Testing

```bash
test/unit_lib.sh       # shell helpers (URL parsing, service file, redaction)
test/e2e.sh            # the full migration against Postgres containers
test/local_e2e.sh      # local/ scripts, with test/fake-render standing in for Render
test/webui_e2e.sh      # the web UI in headless Chromium (needs Playwright)
cd webui && go test ./...
```

`test/e2e.sh` runs preflight (including the no-grant and no-primary-key
failures), schema, sync, a reset and second run, index builds as a detached
job, a watchdog restart, writes during replication, cutover, and cleanup.
It then compares every table by checksum, plus sequences and index sets.
CI runs it for several Heroku/Render version pairs and as both `root` and
`postgres`.

## What testing changed from the original plan

- **Generated columns.** Bucardo 5.6.0 puts generated columns in its COPY
  column list, which Postgres rejects. `start_sync.sh` adds a Bucardo
  `customcols` clause for each affected table, listing only the ordinary
  columns, and Render computes the generated values.
- **`bucardo kick <sync> 0`** returns at the next "sync done" event, which
  can belong to a run that started before writes stopped. `cutover.sh`
  kicks twice, so the second wait covers a run that began after the stop.
- **Render CLI minimum is 2.36.0**, and `--group` takes a group ID, not a
  name. See [docs/spike.md](docs/spike.md) for everything learned from the
  CLI source.
- **Bucardo keeps three file paths in its control database** (reason,
  warning and conflict logs) that default to `/var/log/bucardo`.
  `start_local.sh` moves them to the work directory.
- **Zombie processes.** A sandbox PID 1 that never reaps orphans leaves
  zombies that still answer `kill -0`. Process checks treat zombies as dead.
- **Long steps can run detached.** `local/run.sh --detach build_indexes.sh`
  runs the script with `job.sh` and polls, so a multi-hour index build
  survives a dropped `exec` connection.

## Known limits

- Attended runs only: sandboxes and image import are in beta, and nothing
  supervises the sandbox's processes except the watchdog.
- An imported image can take about a minute to start.
- One-way: writes made to Render after cutover don't flow back to Heroku.
- Indexes are built under Render's collation. If preflight reports a
  collation difference, a person decides whether the sort-order change is
  acceptable.
- Large objects are not migrated.
- The Phase 0 spike has not been run yet. Run `spike/run_spike.sh` and
  record the answers in [docs/spike.md](docs/spike.md) before a real
  migration.
