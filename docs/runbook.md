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
2. **Create sandbox.** It starts from the newest image with no lifetime or
   idle timeout. Its outbound network access is unrestricted, because Render's
   allow-lists admit HTTPS only and would block Postgres. The Sandbox step
   shows the allow-list it would have: the two database hosts, the Slack
   webhook if set, and `api.ipify.org`.
3. **More actions > Diagnostics** shows the sandbox's outbound IP. Allow that
   IP in the Render database's **Inbound IP rules**.
4. **Run preflight.** Fix every blocker and rerun until it passes. Read the
   warnings: a collation difference means someone must accept the change in
   sort order.

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
2. **Tear down** saves a final copy of the artifacts and terminates the
   sandbox. It refuses until the cleanup has succeeded, unless no sync was
   ever started: only **Start sync** puts anything of Bucardo's on Heroku. A
   sandbox that never got that far, for example one that couldn't reach the
   databases, can be torn down without the cleanup check.
3. Keep the Heroku database until you are confident in the new one.

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
