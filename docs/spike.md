# Platform questions

The scripts make a few assumptions about Render sandboxes that only a real
sandbox can confirm. Two things in the web app answer them, no local tooling
needed:

- **The image build log.** Its first lines describe Render's default
  sandbox image: user, arch, kernel, OS, PID 1, CPUs, memory, and whether a
  CA bundle is set.
- **More actions > Diagnostics** on a migration. It runs `diagnose.sh` in the
  migration sandbox and reports:
  - the exec user;
  - PID 1 and zombie processes;
  - whether the create-time environment is visible;
  - whether Bucardo and the watchdog are still alive after the exec that
    started them returned;
  - the outbound IP;
  - TCP reachability of both databases.

Record the answers in the Results section below.

## What each answer decides

| Question | Why it matters | Assumed until confirmed |
|---|---|---|
| Exec user | Postgres refuses to run as root. As root, the scripts re-exec as `postgres` with `runuser`. | Either works; `test/e2e.sh` runs as both `root` and `postgres`. |
| Build sandbox is root or has passwordless sudo | `buildkitd` needs it. | Root. `build_image.sh` fails with a clear message otherwise. |
| Default image has `curl` or `wget`, and `tar` | The build downloads BuildKit. | Yes. |
| Gateway CA trusted in the build sandbox | Credential brokering injects headers into HTTPS, so the broker terminates TLS. BuildKit uses the system trust store (or `SSL_CERT_FILE`). | Trusted by the default image. The build log prints `SSL_CERT_FILE`. |
| Create-time env visible to exec | The connection strings arrive this way. | Visible. The app also writes `/etc/pg-migrate/migration.env`, which `env.sh` loads only if the variables are missing. |
| Detached processes survive their exec | Bucardo and the watchdog must outlive the exec that started them. | They do. Diagnostics shows "Bucardo running: yes" after Start sync. |
| PID 1 reaps orphans | Zombies still answer `kill -0`. | Not reaped; `pid_alive` treats zombies as dead. |
| Outbound IP | It must be allowed in the Render database's inbound IP rules. | Unknown until Diagnostics runs. |
| A long `exec` survives | Not needed: long steps run detached through `job.sh` and the app polls them with short calls. | n/a |

## Answered from the SDK and CLI source

These didn't need a sandbox. They come from `render-oss/sdk` (Python
package `render` 1.9.0) and `render-oss/cli` 2.37.0.

- **Private registries.** `images.import_(..., registry_credentials=...)` takes a
  username and password for one import; Render doesn't store them.
- **Credential brokering.** A credential matches one exact public hostname,
  optionally exact paths and methods. It injects a header, HTTP Basic
  (`user:password`), a cookie, or a query parameter. Secret values are
  write-only and capped at 5 KiB. Creating or changing one needs a
  workspace admin.
- **Images.** `--image`/`image=` takes a name or an ID. A name means "the newest
  succeeded image with that name", so the app passes the exact image ID it
  imported.
- **Sandboxes.** `timeout_seconds=0` means no lifetime limit, and
  `idle_timeout_seconds=0` disables idle suspension. Exec runs
  `bash -c <command>` and streams output, ending with an exit code. A
  non-zero exit is not an exception.
- **Owner.** The SDK needs `RENDER_WORKSPACE_ID` (or `owner_id`) as well as
  `RENDER_API_KEY`.

## Results

Not run against Render yet.
