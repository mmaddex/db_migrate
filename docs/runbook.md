# Runbook

Every step below names the command-line form and the web UI button. They
run the same script. Commands assume `MIGRATION=<name>` is exported, for
example `export MIGRATION=acme`.

## 0. Once per environment

1. **Spike.** Run `spike/run_spike.sh` and commit the answers it writes to
   [spike.md](spike.md). Act on anything that differs from the assumptions
   listed there.
2. **Settings.** Copy `local/config.local.sh.example` to
   `local/config.local.sh`. Set `IMAGE_REPO`, and set `PG_MAJOR` to
   Render's Postgres major version (it must be at least Heroku's).
3. **Tests.** Make sure `test/e2e.sh` passes locally and in CI.
4. **Image.** Build and import it.

   ```bash
   local/build_image.sh
   ```

   ```bash
   local/import_image.sh
   ```

   UI: **Image & environment > Build & push**, then **Import into sandbox group**.

## 1. Prepare a migration

1. Create `migrations/$MIGRATION/migration.env` from `migration.env.example`,
   or use **New migration** in the UI. Use Render's **external** URL.
2. Allow the sandbox to reach both databases on port 5432:
   - Render database **Inbound IP rules**: allow the sandbox's outbound IP
     (the spike records it).
   - Don't set an egress allowlist on the sandbox.
3. Create the sandbox and run preflight. Fix every blocker, then rerun
   preflight until it passes. Read the warnings.

   ```bash
   local/create_sandbox.sh
   ```

   ```bash
   local/run.sh preflight.sh
   ```

   UI: **Create sandbox**, then **Run preflight**.

## 2. Copy and replicate

1. **Freeze schema migrations** on the Heroku app until cutover. A DDL
   change on Heroku is not replicated.
2. Load the schema and start replication:

   ```bash
   local/run.sh schema.sh
   ```

   ```bash
   local/run.sh start_sync.sh
   ```

   UI: **Load schema**, then **Start sync**.
3. Poll status until the phase is `copied`:

   ```bash
   local/status.sh
   ```

   The UI refreshes status on its own every 30 seconds.
4. Build the indexes. This can take hours on a large database, so run it
   detached:

   ```bash
   local/run.sh --detach build_indexes.sh
   ```

   UI: **Build indexes**. The phase becomes `replicating`.
5. Save a copy of the artifacts:

   ```bash
   local/fetch_artifacts.sh
   ```

If the watchdog is not running (status shows it), rerun
`local/run.sh start_sync.sh`. It is idempotent and restarts whatever has
stopped. To start the copy over, run `local/run.sh reset_target.sh --confirm`.

## 3. Rehearse

Run the whole flow, cutover included, against a staging app in a fresh
sandbox from the same image tag. A second migration in the UI works for
this; each one gets its own sandbox.

## 4. Cutover window

1. Turn on Heroku maintenance mode:

   ```bash
   heroku maintenance:on -a <app>
   ```

2. Scale everything that writes to zero (web, workers, clock, schedulers):

   ```bash
   heroku ps:scale web=0 worker=0 -a <app>
   ```

3. Optional: take a runtime snapshot of the sandbox:

   ```bash
   local/snapshot.sh
   ```

4. Cut over:

   ```bash
   local/run.sh cutover.sh --confirm
   ```

   UI: **Cut over**. The dialog asks you to confirm the two steps above
   and type the migration name.

   It waits for replication, compares exact row counts on every table, sets
   sequences, refreshes materialized views, runs ANALYZE and stops Bucardo.
   **If it reports a mismatch, do not switch the app.** Bucardo keeps
   running. Investigate with `local/status.sh --exact` and rerun cutover.
5. Point the app at Render. Use the internal URL if the app runs on Render.
6. Bring the app back up.

## 5. Clean up

1. Clean up Heroku:

   ```bash
   local/run.sh cleanup_source.sh --confirm
   ```

   UI: **Clean up Heroku**. This drops the `bucardo` schema and Bucardo's
   triggers on Heroku. If the sandbox is gone, run it locally:

   ```bash
   local/cleanup_source.sh --confirm
   ```

2. Fetch the artifacts:

   ```bash
   local/fetch_artifacts.sh
   ```

3. Tear down the sandbox:

   ```bash
   local/teardown.sh --confirm
   ```

   UI: **Tear down**. Teardown refuses until cleanup has succeeded.
4. Keep the Heroku database until you are confident in the new one.

## Emergencies

- **A script in the image is wrong mid-run.** Fix it in `sandbox/`, then
  push the fix into the running sandbox:

  ```bash
  local/hotfix.sh sandbox/<script>.sh
  ```

  This logs that the running code no longer matches the image tag. Commit
  the fix and build a new image before the next migration.
- **Bucardo keeps failing.** Status shows "last Bucardo run failed" with
  the reason. The Bucardo log is under **Status > Bucardo log**, or in
  `logs/log.bucardo` in the fetched artifacts.
