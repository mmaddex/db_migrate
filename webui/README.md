# pg-migrate web UI

A local web UI for running Heroku to Render Postgres migrations, several at
once, each in its own Render sandbox. It is a thin layer over the scripts
in `local/`: each button runs one of them with `MIGRATION=<name>` and
streams the output. The UI and the command line share `migrations/<name>/`,
so you can switch between them mid-migration.

```bash
cd webui
go run . -repo ..
```

Then open http://127.0.0.1:7788.

The UI needs the same tools as the scripts: the Render CLI (2.36.0+, logged
in), Docker with buildx for image builds, `jq` and `perl`. **Image &
environment** checks for them.

## What it does

- **New migration** stores the Heroku URL and Render's external URL in
  `migrations/<name>/migration.env` (mode 600). Settings are locked once a
  sandbox exists, because the sandbox keeps the environment it was created
  with.
- **Runbook**: eight steps, from sandbox to teardown. Each step shows
  whether it is done. Buttons are enabled when the step makes sense:
  schema loading needs a passing preflight, and index builds need a
  finished copy. The scripts enforce the same rules on their own.
- Destructive steps (cutover, cleanup, reset, teardown, delete) require you
  to type the migration name. Cutover also asks you to confirm that
  maintenance mode is on and writers are scaled to zero.
- **Status** is refreshed every 30 seconds (`-poll`) by running
  `local/status.sh --json`. It shows the phase from the databases, Bucardo
  and watchdog health, the last sync run, copy and index progress, and
  per-table row counts.
- **Preflight** results are parsed into blockers, warnings and details.
- **Activity** lists the jobs. Their logs are kept in
  `migrations/<name>/jobs/`. One step that changes state runs at a time per
  migration; read-only steps (preflight, fetch, exact counts) can run
  alongside.

## Flags

| Flag | Default | |
|---|---|---|
| `-addr` | `127.0.0.1:7788` | Listen address (`PGM_UI_ADDR`) |
| `-repo` | found from the working directory | Repository root |
| `-migrations` | `<repo>/migrations` | Where migrations live (`MIGRATIONS_DIR`) |
| `-poll` | `30s` | Status refresh interval per running migration |
| `-allow-host` | | Extra `Host` names to accept (`PGM_UI_ALLOW_HOSTS`) |
| `-path-prepend` | | Directory put first on the scripts' PATH (tests use `test/fake-render`) |

## Security

The UI holds database credentials and runs commands, so:

- It listens on loopback by default. It refuses to listen anywhere else
  unless `PGM_UI_PASSWORD` is set, which enables HTTP basic auth.
- It rejects requests whose `Host` isn't localhost or an allowed name,
  which blocks DNS rebinding.
- State-changing requests must be JSON with an `X-PGM-Request: 1` header
  and a same-origin `Origin`, so other websites can't trigger actions.
- It sends a strict Content-Security-Policy and never returns connection
  strings to the browser. Job output is redacted against the migration's
  secrets.

## Fonts

The UI uses PP Neue Montreal when the font files are present and falls back
to Manrope and Roboto Mono. The brand fonts are licensed for internal use,
so they are not in the repository. To use them, copy
`PPNeueMontreal-Variable.woff2` and `PPNeueMontrealMono-Regular.woff2` from
Render's brand kit into `webui/fonts/` (gitignored).

## Tests

```bash
go test -race ./...
```

```bash
../test/webui_e2e.sh
```

`webui_e2e.sh` drives the UI in headless Chromium against the Docker test
harness, using `test/fake-render` in place of the Render CLI. It runs two
migrations side by side: one goes all the way through, and the other is
blocked at preflight.
