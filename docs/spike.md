# Phase 0: platform spike

Run this before trusting the scripts with a real migration. It starts a
minimal Debian image in a Render sandbox and answers the questions that
the image and scripts depend on.

```bash
spike/run_spike.sh
```

The script writes its answers between the result markers below. Commit the
result.

## What each answer decides

| Question | Why it matters | What the scripts assume until the spike runs |
|---|---|---|
| `id` | Postgres refuses to run as root. If exec runs as root, the scripts re-exec themselves as `postgres` with `runuser`. | Either works: both are covered by `test/e2e.sh` (`MIGRATOR_USER=root` and `MIGRATOR_USER=postgres`). |
| `uname -m` | The build platform (`IMAGE_PLATFORM` in `local/config.sh`). | `linux/amd64`. |
| `/etc/os-release` | Confirms the sandbox runs the imported image, not the default base. | The imported image. |
| Dockerfile `ENV` in `env`, ENTRYPOINT ran | If either is ignored, image-level settings are useless. | Neither is used. Every script sources `/opt/migrate/env.sh`. |
| `--env-file` values visible to exec | The connection strings arrive this way. | Visible. If not, copy `migration.env` to `/etc/pg-migrate/migration.env` in the sandbox (`render sandboxes copy`); `env.sh` loads it from there. |
| `nohup` process alive minutes after its exec returned | Bucardo's daemon and the watchdog must outlive the exec that started them. | Alive. They are started with `setsid nohup ... &` and all inherited descriptors closed. |
| Zombies / PID 1 reaping | A PID 1 that never reaps leaves zombie processes, which still answer `kill -0`. | Not reaped. `pid_alive` treats zombies as dead. |
| exec umask | Files written by an exec session. | May be `000`; `env.sh` sets `umask 022`. |
| Sandbox create to running | Sizing the polling timeout in `create_sandbox.sh`. | Up to about a minute for an imported image; polls for 15 minutes. |
| Outbound IP | Must be allowed in the Render database's inbound IP rules. Don't set an egress allowlist on the sandbox. | Unknown; set per migration. |
| Long exec (optional, `SPIKE_LONG_EXEC=1800`) | Whether a multi-hour `render sandboxes exec` survives. | Unknown. Use `local/run.sh --detach` for `build_indexes.sh` (runs as a detached job and polls). |

## Answered from the Render CLI source (v2.37.0)

These did not need a sandbox. They come from reading
[`render-oss/cli`](https://github.com/render-oss/cli) at the 2.37.0 release:

- **Private registries:** `render sandbox-groups images import` takes
  `--registry-username` and `--registry-password-stdin`. The password is
  used for that import only and is not stored. `local/import_image.sh`
  uses these flags when `REGISTRY_USERNAME`/`REGISTRY_PASSWORD` are set.
  The image holds no secrets, so publishing it publicly also works.
- **Minimum CLI version is 2.36.0, not 2.34.0.** 2.34.0 added image import
  and `--image`, but `sandboxes create --name` and `sandboxes list --name`
  arrived in 2.36.0. `local/config.sh` enforces 2.36.0.
- **`--group` takes a group ID** (`sbg-...`), not a name. The scripts
  resolve `SANDBOX_GROUP` (a name) to its ID with `sandbox-groups list`.
- **`--image` takes a name or an ID.** A name means "the newest succeeded
  image with that name", so the scripts pass the recorded image ID
  (`sbb-...`) for the exact tag they imported.
- **Waits:** `sandboxes create --wait` gives up after 60 seconds and
  `images import --wait` after 10 minutes. Neither cancels the operation,
  so both scripts poll instead.
- **`sandboxes exec`** joins its arguments into one shell command, streams
  stdout and stderr, and exits with the remote command's exit code. It has
  no stdin.
- **`sandboxes copy`** transfers directories as archives. A relative remote
  path resolves inside the sandbox's home directory.
- **`--idle-timeout=0`** disables idle suspension. **`--timeout=0`** means no
  lifetime limit (since 2.34.0).
- In 2.37.0, `sandboxes list` opens an interactive list in a terminal; the
  scripts always pass `-o json`.

## Results

<!-- spike-results:start -->
Not run yet. Run `spike/run_spike.sh` with a Render API key and an
`IMAGE_REPO` you can push to.
<!-- spike-results:end -->
