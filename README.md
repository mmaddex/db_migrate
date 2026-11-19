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
   and a small Postgres for the app's own state. The service has Render's
   authentication enabled (`authentication: enabled: true`). That signs users
   in and forwards their access token, which the app verifies on every
   request (see Access below).
2. Render asks for these when it creates the Blueprint. To change them later,
   open the web service's **Environment**:

   | Variable | Value |
   |---|---|
   | `AUTH_ISSUER` | Issuer of the access tokens your identity provider sends, e.g. `https://<org>.okta.com` (see Access below) |
   | `RENDER_API_KEY` | API key of a **workspace admin**: creating brokered credentials needs admin |
   | `RENDER_WORKSPACE_ID` | The workspace the sandboxes belong to (`tea-...`) |
   | `GITHUB_REGISTRY` | e.g. `ghcr.io/your-org` (the image becomes `ghcr.io/your-org/pg-migrate:<commit>`) |
   | `GITHUB_USERNAME` | The GitHub user the token belongs to |
   | `GITHUB_TOKEN` | Classic PAT with `write:packages` (includes `read:packages`) |

   `APP_SECRET_KEY` is generated for you. It encrypts stored connection
   strings, so don't change it once migrations exist.

   `SANDBOX_IDLE_TIMEOUT_SECONDS` (default `300`, set in `render.yaml`) is how
   long a migration sandbox waits with no step running before it suspends.
   See Suspending between steps below.

   The image covers Postgres 14 to 18 on either side, so you don't set a
   version per migration. It holds client tools for each of
   `PG_CLIENT_MAJORS` (default `14 15 16 17 18`), and the schema step dumps
   with the target's version, or the source's if the target is older.
   Preflight reports which one it will use. To support another major, add it
   to `PG_CLIENT_MAJORS` and rebuild the image. `PG_MAJOR` (default 17) is
   only the version of the sandbox's own Postgres, which holds Bucardo's
   control database, so it doesn't have to match either database.
3. Decide on sandbox groups. You don't have to create any: the app looks up
   its two groups by name in `RENDER_WORKSPACE_ID`, and creates a missing
   one the first time it needs it.

   | Group | Default name | Setting | Holds |
   |---|---|---|---|
   | Migration group | `pg-migration` | `SANDBOX_GROUP` | One sandbox per migration, and the imported image they start from |
   | Build group | `pg-migration-build` | `BUILD_SANDBOX_GROUP` | The image-build sandbox and the brokered GitHub credential |

   - **To use groups you already have,** set those variables to the groups'
     names, not their `sbg-...` IDs. **Image & settings > Configuration** shows
     which groups the app is using.
   - **To choose the region,** create the group yourself in that region before
     the first build, under the name the app looks for. A group's region
     decides where its sandboxes run, and the app creates groups without
     choosing a region.
   - **Keep the two groups separate.** The build group's credential adds your
     GitHub token to registry requests from every sandbox in that group.
   - **If you change `SANDBOX_GROUP` later,** select **Import into sandbox
     group** (or rebuild) before creating sandboxes. Imported images belong
     to a group, and existing sandboxes stay in their old group.
   - **Outbound network access** is set on each sandbox when the app creates
     it, as an allow-list of hosts. A sandbox can reach nothing else.
     - **The build sandbox** uses `BUILD_NETWORK_POLICY`. `render.yaml` sets it
       to every host the build contacts: `github.com` and
       `release-assets.githubusercontent.com` (BuildKit), `mirror.gcr.io`
       (base image), `deb.debian.org` and `apt.postgresql.org` (packages, over
       HTTPS), and `ghcr.io` (push). If you change `BASE_IMAGE` or
       `GITHUB_REGISTRY`, add their hosts. It also takes `allow-all` or
       `deny-all`.
     - **Each migration sandbox** is `allow-all` for now. Render's
       allow-lists admit HTTPS only, so one would block the sandbox's
       Postgres connections; a sandbox created under one reported both
       databases unreachable. The app still works out the allow-list each
       sandbox should have, from that migration's settings: the hosts of its
       Heroku and Render database URLs, its Slack webhook's host if it has
       one, and `api.ipify.org` (Diagnostics uses it to find the sandbox's
       outbound IP). Bucardo needs nothing else outside the sandbox. The list
       is logged when the sandbox is created and shown on the migration's
       page. Once Render can allow-list Postgres, set
       `ENFORCE_MIGRATION_ALLOW_LIST = True` in `app/pgmigrate/config.py`.
       Database hosts must be DNS names, not IP addresses: the app refuses a
       URL whose host couldn't go in an allow-list.
4. Make sure credential brokering is enabled for the workspace. It's in
   alpha and Render switches it on per workspace; without it the build stops
   when it saves the credential, with a 404 from Render.
5. Open the app and go to **Image & settings > Build & push image**.
6. Select **New migration** and follow the runbook. See [docs/runbook.md](docs/runbook.md).

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
   without a key Bucardo can use, missing extensions, unreachable databases,
   or a source newer than the image's client tools.
   - **Replication keys.** A table needs a primary key, or a unique index
     without a predicate or expressions whose columns hold no NULLs. For the
     others, `keys.sh` writes SQL that adds a surrogate key without locking
     the table. The runbook shows it to copy into `heroku pg:psql`, or runs
     it, and later removes the key the same way. See
     [docs/runbook.md](docs/runbook.md#replication-keys).
   - **Render superuser.** Render's default database user can't set
     `session_replication_role`. Preflight then shows SQL to create
     `migration_super_user`, and the SQL Render support runs to make it a
     superuser. Its URL goes in the migration's optional Render superuser
     URL. Every step then logs in as it, and the Render database URL stays
     the app's own credential.
2. **Schema** loads the Heroku schema into Render *without* its
   `CREATE INDEX` statements. Constraints stay, and so do the unique indexes
   that foreign keys or Bucardo's keys depend on.
3. **Start sync** configures one Bucardo relgroup and one sync with
   `onetimecopy=1`. Bucardo copies everything, then replicates changes. A
   watchdog restarts Bucardo or the local Postgres if either stops.
4. **Build indexes** creates every deferred index `CONCURRENTLY` on Render
   after the copy. Getsafe measured more than 100x faster copies this way.
5. **Cutover**, after Heroku writes stop, does six things:
   - waits for a sync that started after the stop;
   - requires exact row counts to match on every table;
   - sets sequences;
   - refreshes materialized views and runs ANALYZE;
   - with a Render superuser URL, gives everything that user created
     (schemas, tables, sequences, views, extensions) to the user in the
     Render database URL, so the app can use it;
   - stops Bucardo.
6. **Clean up Heroku** drops the `bucardo` schema and its triggers.
   **Tear down** saves the artifacts and terminates the sandbox.

Every step is a job. Scripts run detached inside the sandbox (`job.sh`);
the app tails their logs and checkpoints progress in Postgres. A deploy or
restart of the web app therefore resumes running jobs instead of losing
them. Status (phase, row counts, copy and index progress) comes from the
databases, refreshed every 30 seconds while a step runs or Bucardo
replicates.

### Suspending between steps

A migration sandbox suspends when it's idle between steps, so you don't
pay for it while nothing happens:

- **How it suspends.** Render has no suspend call. Instead, the app creates
  each sandbox with an idle timeout, `SANDBOX_IDLE_TIMEOUT_SECONDS`
  (default 300, Render's minimum). Render suspends a sandbox after that long
  without a command or file transfer. A suspended sandbox keeps its files
  and network policy.
- **How it wakes.** Every step, Fetch artifacts, Snapshot and Tear down wake
  the sandbox first. Render resumes a sandbox on its next command, so the
  app runs `true` and waits for it to be running. A rerun of Create sandbox
  reuses a suspended sandbox rather than starting a second one.
- **When it stays awake.** Processes left running in the background don't
  count as activity, so the app keeps the sandbox busy with status checks:
  - while a step runs (its own polling does this too);
  - from **Start sync** until a **Cut over** that succeeds, or **Reset Render
    target**, because Bucardo must keep replicating. If the sandbox suspends
    anyway, for example because the web app was down for longer than the
    timeout, the next status check wakes it.

  At other times, the status check after a step reads the step's results,
  and then the app only reads the sandbox's state from the API. The Status
  panel keeps the last result and says the sandbox is suspended.
- **Caveats.**
  - The idle timeout is fixed when a sandbox is created, so a change
    applies to new sandboxes only. Sandboxes created before this feature
    never suspend.
  - Whether processes survive a suspension is still unconfirmed (see
    [docs/spike.md](docs/spike.md)). If they don't, a step interrupted by
    one fails and can be rerun, and a stopped Bucardo shows as *Needs
    attention*; **Restart Bucardo / watchdog** fixes that.
  - Set the timeout to `0` to keep sandboxes running between steps.

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
- See Access below for how every request is authenticated.

## Access

Every request except `/healthz` must carry a valid access token in
`X-Forwarded-Access-Token`, which Render's service authentication adds after
sign-in. The app checks it itself, in one of two ways (`AUTH_VERIFY`):

- **`jwt`** (default): the token must be a JWT. The app checks:
  - the signature, against the issuer's published keys, rotation included;
  - expiry, issuer, and audience when one is configured;
  - the algorithm, which must be on an asymmetric allow-list, so `none` and
    HMAC are rejected.

  Okta's org authorization server and Entra ID send JWTs.
- **`userinfo`**: the app sends the token to the issuer's userinfo endpoint,
  which says whether it's valid and whose it is. Use this when the access
  token isn't a JWT. Auth0, for example, sends an opaque or encrypted token
  unless the login asks for an API audience. Each answer is reused for 5
  minutes, so a user who loses access can keep it for up to 5 more minutes.
  The audience can't be checked this way.

A request that reaches the service without a valid token is refused,
including one over Render's private network. Anyone signed in is allowed in
unless `AUTH_ALLOWED_USERS` narrows it (users or `@domains`). The signed-in
user is shown in the top bar and recorded on every job and migration.

It works with any OpenID Connect provider:

| Setting | Default | |
|---|---|---|
| `AUTH_ISSUER` | (required) | e.g. `https://render.okta.com`, `https://<tenant>.auth0.com/`, `https://login.microsoftonline.com/<tenant>/v2.0` |
| `AUTH_VERIFY` | `jwt` | `jwt` or `userinfo` (see above) |
| `AUTH_JWKS_URL` | from `<issuer>/.well-known/openid-configuration` | Signing keys |
| `AUTH_USERINFO_URL` | from the same discovery document | Used by `userinfo`, and to show the user's name |
| `AUTH_AUDIENCE` | unset (not checked) | Required `aud`; `jwt` only |
| `AUTH_ALGORITHMS` | `RS256` | Asymmetric algorithms only |
| `AUTH_TOKEN_HEADER` | `X-Forwarded-Access-Token` | |
| `AUTH_USER_CLAIM` | `sub` | Claim that names the user, e.g. `email`. With `email`, an unverified email is refused |
| `AUTH_ALLOWED_USERS` | unset (anyone signed in) | Comma list of users and `@domains` |

For Auth0, set `AUTH_ISSUER=https://<tenant>.auth0.com/` (the trailing
slash is part of Auth0's issuer), `AUTH_VERIFY=userinfo` and
`AUTH_USER_CLAIM=email`. Auth0's `sub` is an ID like `auth0|64f...`.

The log shows the settings at startup and one line per refused request:
the reason, the kind of token that arrived (JWT with its algorithm, issuer
and audience; encrypted; or opaque), and which login headers were present.
It never logs the token itself. The user's first request each hour is
logged as a sign-in. With no `AUTH_ISSUER` the app refuses to start;
`PGM_DEV=1` turns authentication off for local development only, and is
ignored on Render. State-changing requests also need a custom header and a
same-origin `Origin`. The platform adds the token to any request from a
signed-in browser, so these checks stop cross-site request forgery.

## Layout

| Path | What |
|---|---|
| `app/` | The web app: Starlette, the Render Python SDK, Postgres for state, token verification |
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
- **Keys without a primary key.** `bucardo add all tables` silently skips a
  table with no primary key and no unique index. For a table without a
  primary key, Bucardo uses a unique index with no predicate and no
  expressions: the one with the fewest columns, the newest on a tie. It
  doesn't check that the index is valid or its columns NOT NULL, so
  preflight does. With its default strict checking, Bucardo refuses a sync
  where a column has a DEFAULT on one side only, so a surrogate key must
  reach Render with its default. Adding it before Load schema does that.

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
