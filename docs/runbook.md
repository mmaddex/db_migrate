# Runbook

Everything happens in the web app. Each step is a button on the migration's
page; its output streams into **Activity** and stays there.

## 0. Once

1. Deploy the Blueprint and set the environment variables (see the README).
2. **Image & settings > Build & push image.** This takes a few minutes. When
   it finishes, the image shows as `ready` and new sandboxes start from it.
   Rebuild after deploying changes to `sandbox/` or `image/`.
3. Read the build log's first lines (`[build] user=... arch=... os=...`).
   Record anything surprising in [spike.md](spike.md).

## 1. Prepare a migration

1. **New migration**: a name, the Heroku `DATABASE_URL`
   (`heroku config:get DATABASE_URL -a <app>`), and Render's **external**
   database URL. Schemas default to `public`.
2. **Create sandbox.** It starts from the newest image with no lifetime
   limit. Between steps it suspends after 5 minutes idle
   (`SANDBOX_IDLE_TIMEOUT_SECONDS`), and each step wakes it. It stays awake
   from Start sync until cutover, while Bucardo replicates. Its outbound
   network access is unrestricted, because Render's allow-lists admit HTTPS
   only and would block Postgres. The Sandbox step shows the allow-list it
   would have: the two database hosts, the Slack webhook if set, and
   `api.ipify.org`.
3. **More actions > Diagnostics** shows the sandbox's outbound IP. Allow that
   IP in the Render database's **Inbound IP rules**.
4. **Run preflight.** Fix every blocker and rerun until it passes. Read the
   warnings: a collation difference means someone must accept the change in
   sort order.
5. **Replication keys**, only if preflight reports tables without a primary
   key. See below.

### Replication keys

Bucardo needs a key on every table. A primary key works, and so does a
unique index without a `WHERE` clause or expressions. Without either,
Bucardo leaves the table out. Preflight accepts a table with such an index if
none of its key columns holds a NULL. It reports the index Bucardo will use.
It blocks the other tables, and the **Replication keys** step appears.

The step lists each blocked table and why, and shows SQL that gives each one
a surrogate key without locking it:

- a `bigint` column `mig_id` (`mig_id_1` and so on if the name is taken),
  added with no default, so the table isn't rewritten;
- a sequence default for new rows, set as its own statement;
- a backfill of the existing rows: one pass over the table's pages, 100 per
  transaction, then the rows the app updated meanwhile;
- a unique index built with `CREATE INDEX CONCURRENTLY`, then `VACUUM
  (ANALYZE)` for the dead rows the backfill leaves behind.

The `ALTER TABLE` statements run with a 5-second `lock_timeout`, so they
don't queue other queries behind a long transaction. The column gets a
comment, so pg-migrate can find it again later.

Run the SQL one of two ways:

- **Copy SQL** and run it in `heroku pg:psql -a <app>`. Use psql, not a
  client that wraps statements in a transaction: the backfill and
  `CREATE INDEX CONCURRENTLY` commit as they go. Then run preflight again.
- **Run it for me** runs it on Heroku with the migration's credentials. The
  dialog shows the SQL. The app runs only that SQL: if Heroku changed since
  preflight, the step stops and asks for a fresh preflight. Activity shows
  each statement with its result and time, and progress while a large table
  fills. Preflight then runs again by itself.

Either way the SQL is safe to run again if it stops partway, for example on
a lock timeout. Add the keys before **Load schema**: the schema step copies
the column, its default and its index to Render, so the two sides match
(Bucardo refuses to sync a column whose default differs between them) and
Render can find rows by key from the first replicated change. Bucardo copies
the values from Heroku.

Before you run it:

- **Large tables.** The backfill writes every row once. That's a lot of WAL,
  and Heroku followers lag while it runs.
- **Triggers.** The step warns about a table with `UPDATE` triggers: the
  backfill fires them once per row. Check that they don't change data, such
  as an `updated_at` column.
- **Ownership.** Run it for me needs the Heroku role to own the table. If it
  doesn't, the table's owner runs the SQL.
- **The app.** The new column shows up in `SELECT *`. Code that maps columns
  by position, and exports or `COPY` jobs that expect a fixed set of
  columns, see an extra field until you remove it. `INSERT INTO t VALUES
  (...)` without a column list still works: the column is last, and it gets
  its default.

Partitioned tables, partitions and tables that use inheritance get no SQL.
Give them a key by hand.

## 2. Copy and replicate

1. **Freeze schema migrations** on the Heroku app until cutover. DDL is not
   replicated.
2. **Load schema**, then **Start sync**. Status shows `Copying` until the
   initial copy is done, then `Copied`.
3. **Build indexes.** It can take hours on a large database. It runs detached
   in the sandbox, so redeploying or restarting the app doesn't stop it.
   The phase becomes `Replicating`.
4. **More actions > Fetch artifacts** saves the schema dumps and reports as a
   download.

If Status says *Needs attention* because Bucardo or the watchdog stopped,
**More actions > Restart Bucardo / watchdog** reruns `start_sync.sh`, which
starts whatever stopped. To start the copy over, use **Reset Render target**.

## 3. Rehearse

Run the whole flow, cutover included, against a staging app as a second
migration. Each migration gets its own sandbox.

## 4. Cutover window

1. Turn on Heroku maintenance mode:

   ```bash
   heroku maintenance:on -a <app>
   ```

2. Scale everything that writes to zero:

   ```bash
   heroku ps:scale web=0 worker=0 -a <app>
   ```

3. Optional: **More actions > Snapshot sandbox**.
4. **Cut over.** The dialog asks you to confirm the two steps above and type
   the migration name. The step does five things:
   - waits for replication;
   - compares exact row counts on every table;
   - sets sequences;
   - refreshes materialized views and runs ANALYZE;
   - stops Bucardo.

   If it reports a `MISMATCH`, **do not switch the app**. Bucardo keeps
   running. Investigate with **Exact row counts**, then run Cut over again.
5. Point the app at Render. Use the internal URL if the app runs on Render.
6. Bring the app back up.

## 5. Clean up

1. **Clean up Heroku** drops the `bucardo` schema and Bucardo's triggers on
   Heroku (and on Render, if present).
2. Optional: **Remove replication keys**, if pg-migrate added any. A unique
   surrogate key is usually harmless, and it helps if you later use logical
   replication, which needs a key to replicate updates and deletes. Keep it
   unless something depends on the table having no key or on its exact
   columns. Otherwise copy the SQL, or let the app run it on Heroku and then
   Render. It drops each index concurrently, then its column and the sequence
   the column owns. `DROP COLUMN` takes a brief exclusive lock, so it runs
   with a 5-second `lock_timeout`. The step waits for Clean up Heroku because
   Bucardo's triggers on Heroku use the column: drop it first and every write
   to the table fails. Do this before Tear down, or run the SQL yourself
   afterwards.
3. **Tear down** saves a final copy of the artifacts and terminates the
   sandbox. It refuses until the cleanup has succeeded, unless no sync was
   ever started: only **Start sync** puts anything of Bucardo's on Heroku. A
   sandbox that never got that far, for example one that couldn't reach the
   databases, can be torn down without the cleanup check.
4. Keep the Heroku database until you are confident in the new one.

## More actions

These buttons are under the runbook on a migration's page. The **i** beside
each one shows a short version of this list. Only Restart and Reset run
Bucardo commands, and Exact row counts prints Bucardo's status. The scripts
are in `sandbox/`.

- **Fetch artifacts** copies `/var/lib/migrate` out of the sandbox (`logs/`,
  `artifacts/`, `state/` and `jobs/`) with Render's
  [file download](https://render.com/docs/sandboxes-files), and saves it
  under Artifacts as a `.tar.gz`, up to 200 MB. It runs no script.
- **Exact row counts** runs `status.sh --exact`: `SELECT count(*)` on every
  table on Heroku and on Render. Status shows estimates. This reads every
  row, so it is slow on large tables. If Bucardo is running, it also prints
  `bucardo status`
  ([viewing information](https://bucardo.org/Bucardo/cli/#viewing-information))
  and the end of the Bucardo log.
- **Snapshot sandbox** takes a Render
  [runtime snapshot](https://render.com/docs/sandboxes-snapshots) of the
  sandbox: its disk, memory and running processes. To restore it, create a
  new sandbox from the snapshot ID in the log.
- **Diagnostics** runs `diagnose.sh`, which changes nothing. It shows the
  sandbox's outbound IP for the Render database's inbound IP rules, whether
  each database's port accepts a TCP connection, and whether the control
  Postgres, Bucardo and the watchdog are running. See [spike.md](spike.md).
- **Restart Bucardo / watchdog** reruns `start_sync.sh`, the Start sync
  script. It skips each step that is already done:
  1. Start the local control Postgres, which holds Bucardo's configuration.
  2. `bucardo add db` for Heroku and Render
     ([add database](https://bucardo.org/Bucardo/cli/add_database)).
  3. `bucardo add all tables` into one relgroup
     ([adding tables](https://bucardo.org/Bucardo/cli/#adding-tables),
     [relgroup](https://bucardo.org/Bucardo/object_types/relgroup)).
  4. `bucardo add sync` with `onetimecopy=1`
     ([add sync](https://bucardo.org/Bucardo/cli/add_sync),
     [onetimecopy](https://bucardo.org/Bucardo/operations/onetimecopy)).
  5. `bucardo add customcols` for tables with generated columns. The
     comment in `start_sync.sh` says why.
  6. `bucardo start` if Bucardo isn't running
     ([starting Bucardo](https://bucardo.org/Bucardo/cli/#starting-bucardo)).
  7. Start the watchdog if it isn't running.

  On a restart, steps 2 to 5 are already done. Bucardo sets `onetimecopy`
  back to 0 when the initial copy finishes, so a restart after that resumes
  replicating changes and doesn't copy again. The watchdog (`watchdog.sh`)
  is part of pg-migrate, not Bucardo: every minute it restarts the control
  Postgres or Bucardo if either has stopped. The button is enabled from Start
  sync until cutover.
- **Reset Render target** runs `reset_target.sh --confirm`. It stops the
  watchdog, runs `bucardo stop`
  ([stopping Bucardo](https://bucardo.org/Bucardo/cli/#stopping-bucardo)),
  truncates every migrated table on Render, and drops the indexes Build
  indexes created. Then it drops the `bucardo` schema on Render and the local
  `bucardo` database, which holds all of Bucardo's configuration; Start sync
  creates it again. Bucardo's triggers on Heroku stay, so run Clean up Heroku
  if you aren't copying again soon. It refuses after cutover.
- **Delete migration** removes the migration from the app: its stored
  connection strings, job logs and downloaded artifacts. It doesn't touch
  either database. It's enabled when the migration has no sandbox, or after
  Tear down.

## If something goes wrong

- **A step failed.** Its log is under Activity. Steps are safe to rerun:
  every script is idempotent, and destructive ones ask for confirmation.
- **Bucardo keeps failing.** Status shows "last Bucardo run failed" with the
  reason. The Bucardo log is under Status.
- **The web app was redeployed mid-step.** Nothing to do: the job resumes
  and its log says so.
- **A script in the image is wrong.** Fix it in `sandbox/`, deploy, rebuild
  the image, and start a new sandbox from it (Reset Render target or a new
  migration). A running sandbox keeps the image it started with.
