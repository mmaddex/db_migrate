#!/usr/bin/env bash
# Phase 0 platform spike. Builds and pushes a minimal image, imports it,
# starts a sandbox with both timeouts at 0, asks the questions in
# docs/spike.md, and writes the answers between the result markers there.
#
#   spike/run_spike.sh                 full run (includes a 5 minute wait)
#   SPIKE_NOHUP_WAIT=60 spike/run_spike.sh
#   SPIKE_LONG_EXEC=1800 spike/run_spike.sh   also time a 30 minute exec
#   SPIKE_KEEP=1 spike/run_spike.sh           leave the sandbox running
#
# Optional: SPIKE_DB_HOSTS="host1 host2" checks TCP 5432 reachability.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=local/config.sh
. local/config.sh
require_render
command -v docker >/dev/null 2>&1 || die "docker is required"
case "$IMAGE_REPO" in *CHANGE-ME*) die "set IMAGE_REPO in local/config.local.sh" ;; esac

TAG="spike-$(date -u +%Y%m%d%H%M%S)"
REF="$IMAGE_REPO:$TAG"
OUT="$(mktemp)"; export OUT
trap 'rm -f "$OUT" "${ENVF:-}"' EXIT
r() { printf '%s\n' "$*" >>"$OUT"; }

log "building and pushing $REF ($IMAGE_PLATFORM)"
docker buildx build --platform "$IMAGE_PLATFORM" --provenance=false --sbom=false \
  -f spike/Dockerfile ${BASE_IMAGE:+--build-arg "BASE_IMAGE=$BASE_IMAGE"} -t "$REF" --push spike >/dev/null

gid="$(group_id)"
if [ -z "$gid" ]; then gid="$(render_json sandbox-groups create --name "$SANDBOX_GROUP" | jq -r .id)"; fi

log "importing (private registry flags available: $(render sandbox-groups images import --help 2>&1 | grep -c -- '--registry-username'))"
imp_start=$(date +%s)
imp="$(render_json sandbox-groups images import "$REF" --name pg-migrate-spike --group "$gid" --wait || true)"
img_id="$(jq -r '.id // empty' <<<"$imp")"
while [ "$(jq -r '.status // empty' <<<"$imp")" != succeeded ]; do
  case "$(jq -r '.status // empty' <<<"$imp")" in failed) die "import failed: $(jq -c . <<<"$imp")" ;; esac
  sleep 10; imp="$(render_json sandbox-groups images get "$img_id" --group "$gid")"
done
imp_secs=$(( $(date +%s) - imp_start ))

ENVF="$(mktemp)"
printf 'SPIKE_ENV_FILE_VAR=present\n' >"$ENVF"
log "creating sandbox"
c_start=$(date +%s)
sb="$(render_json sandboxes create --group "$gid" --image "$img_id" --name pg-migrate-spike \
  --timeout=0 --idle-timeout=0 --plan "$SANDBOX_PLAN" --env-file "$ENVF")"
id="$(jq -r .id <<<"$sb")"
while [ "$(render_json sandboxes list --name pg-migrate-spike --all | jq -r --arg id "$id" '.[] | select(.id==$id) | .status')" != running ]; do
  sleep 2
done
c_secs=$(( $(date +%s) - c_start ))
x() { render sandboxes exec "$id" -- "$@" 2>&1; }

r "Run: $(ts), CLI $(render --version 2>/dev/null | head -n1), plan $SANDBOX_PLAN, image $REF"
r ""
r "| Question | Answer |"
r "|---|---|"
r "| \`id\` | \`$(x id | tr -d '\n')\` |"
r "| \`uname -m\` | \`$(x uname -m | tr -d '\n')\` |"
r "| \`/etc/os-release\` | \`$(x sh -c '. /etc/os-release; echo $PRETTY_NAME' | tr -d '\n')\` |"
r "| Dockerfile \`ENV\` visible in \`env\` | $(x env | grep -q '^SPIKE_DOCKERFILE_ENV=present' && echo yes || echo no) |"
r "| ENTRYPOINT ran | $(x test -e /tmp/spike-entrypoint-ran && echo yes || echo no) |"
r "| PID 1 | \`$(x sh -c 'tr "\0" " " </proc/1/cmdline' | tr -d '\n')\` |"
r "| \`--env-file\` values visible to exec | $(x env | grep -q '^SPIKE_ENV_FILE_VAR=present' && echo yes || echo no) |"
r "| exec PATH | \`$(x sh -c 'echo $PATH' | tr -d '\n')\` |"
r "| exec umask / HOME / cwd | \`$(x sh -c 'echo "$(umask) $HOME $(pwd)"' | tr -d '\n')\` |"
r "| /var/run is tmpfs | $(x sh -c 'df -T /run 2>/dev/null | tail -n1' | awk '{print $2}') |"
r "| Outbound IP (for Render inbound IP rules) | \`$(x curl -fsS -m 10 https://api.ipify.org 2>/dev/null || echo unknown)\` |"
r "| Image import time | ${imp_secs}s |"
r "| Sandbox create to running | ${c_secs}s |"
for h in ${SPIKE_DB_HOSTS:-}; do
  r "| TCP 5432 to $h | $(x timeout 5 bash -c "</dev/tcp/$h/5432" >/dev/null 2>&1 && echo reachable || echo blocked) |"
done

log "nohup test: starting a detached sleep, then waiting ${SPIKE_NOHUP_WAIT:-300}s"
pid="$(x sh -c 'setsid nohup sleep 100000 >/dev/null 2>&1 </dev/null & echo $!' | tr -dc '0-9')"
x sh -c '(sleep 1 &) ; true' >/dev/null   # an orphan, to see whether PID 1 reaps it
sleep "${SPIKE_NOHUP_WAIT:-300}"
r "| nohup process alive ${SPIKE_NOHUP_WAIT:-300}s after its exec returned | $(x ps -o stat= -p "$pid" | grep -qv '^Z' && echo yes || echo no) |"
r "| Zombies present (PID 1 does not reap orphans) | $(x sh -c 'ps -eo stat= | grep -c "^Z"' | tr -d '\n') |"

if [ -n "${SPIKE_LONG_EXEC:-}" ]; then
  log "long exec test: sleep $SPIKE_LONG_EXEC"
  le_start=$(date +%s); le_rc=0
  render sandboxes exec "$id" -- sleep "$SPIKE_LONG_EXEC" || le_rc=$?
  r "| exec of sleep $SPIKE_LONG_EXEC | exit $le_rc after $(( $(date +%s) - le_start ))s |"
fi

if [ -z "${SPIKE_KEEP:-}" ]; then
  render sandboxes stop "$id" --confirm >/dev/null || warn "could not stop $id"
fi

perl -0pi -e '
  BEGIN { local $/; open my $f, "<", $ENV{OUT} or die; $r = <$f> }
  s/(<!-- spike-results:start -->\n).*?(<!-- spike-results:end -->)/$1$r$2/s
' docs/spike.md
log "results written to docs/spike.md"
cat "$OUT"
