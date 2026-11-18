# shellcheck shell=bash
#
# Helpers for the local/ scripts. Sourced by config.sh.
# Bash 3.2 compatible (macOS): no associative arrays, mapfile or ${x,,};
# guard empty arrays as ${a[@]+"${a[@]}"}.

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log()  { printf '%s [%s] %s\n' "$(ts)" "$(basename "$0" .sh)" "$*" >&2; }
warn() { printf '%s [%s] WARN %s\n' "$(ts)" "$(basename "$0" .sh)" "$*" >&2; }
die()  { printf '%s [%s] ERROR %s\n' "$(ts)" "$(basename "$0" .sh)" "$*" >&2; exit 1; }

has_flag() {
  local flag="$1" a; shift
  for a in "$@"; do [ "$a" = "$flag" ] && return 0; done
  return 1
}
require_confirm() {
  has_flag --confirm "$@" || die "this step is destructive; rerun with --confirm"
}

# Load KEY=VALUE lines into shell variables without evaluating them.
# Variables already set in the environment win.
load_conf() {
  local file="$1" line key value
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    key="${line%%=*}"; value="${line#*=}"
    [ "$key" != "$line" ] || continue
    case "$key" in *[!A-Za-z0-9_]*|[0-9]*|'') continue ;; esac
    case "$value" in
      \"*\") value="${value#\"}"; value="${value%\"}" ;;
      \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    if [ -z "$(eval "printf '%s' \"\${$key:-}\"")" ]; then
      printf -v "$key" '%s' "$value"
    fi
  done <"$file"
}

# Does an env file define KEY with a non-empty value? Never prints values.
env_file_has() {
  grep -Eq "^(export )?$2=.+" "$1"
}

# ------------------------------------------------------------ render CLI

version_ge() {
  # version_ge A B  -> true if A >= B (numeric X.Y.Z)
  awk -v a="$1" -v b="$2" 'BEGIN {
    split(a, x, "."); split(b, y, ".");
    for (i = 1; i <= 3; i++) { if (x[i] + 0 > y[i] + 0) exit 0; if (x[i] + 0 < y[i] + 0) exit 1 }
    exit 0 }'
}

require_render() {
  command -v render >/dev/null 2>&1 || die "the Render CLI is not installed (https://render.com/docs/cli)"
  command -v jq >/dev/null 2>&1 || die "jq is required"
  local v
  v="$(render --version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n1)"
  [ -n "$v" ] || die "could not read the Render CLI version"
  version_ge "$v" "$RENDER_MIN_VERSION" || die "Render CLI $v is too old; need $RENDER_MIN_VERSION or later (render update)"
}

# render_json ARGS... -> runs `render ARGS -o json`, prints stdout, fails
# with the CLI's stderr on error.
render_json() {
  local out err rc=0
  err="$(mktemp)"
  out="$(render "$@" -o json 2>"$err")" || rc=$?
  if [ $rc -ne 0 ]; then
    cat "$err" >&2; rm -f "$err"
    [ -n "$out" ] && printf '%s\n' "$out"
    return $rc
  fi
  rm -f "$err"
  printf '%s\n' "$out"
}

# Sandbox group ID for $SANDBOX_GROUP, or empty.
group_id() {
  render_json sandbox-groups list | jq -r --arg n "$SANDBOX_GROUP" '[.[] | select(.name == $n)] | first | .id // empty'
}

require_group_id() {
  local gid
  gid="$(group_id)" || die "could not list sandbox groups (is the CLI logged in? render login / RENDER_API_KEY)"
  [ -n "$gid" ] || die "sandbox group $SANDBOX_GROUP does not exist; run local/import_image.sh first"
  printf '%s\n' "$gid"
}

# Sandbox ID saved by create_sandbox.sh.
sandbox_id() {
  [ -s "$SANDBOX_ID_FILE" ] || die "no sandbox for migration '$MIGRATION' (no $SANDBOX_ID_FILE); run local/create_sandbox.sh"
  tr -d '[:space:]' <"$SANDBOX_ID_FILE"
}

# JSON object for one sandbox ID (any status), or empty.
sandbox_json() {
  render_json sandboxes list --name "$SANDBOX_NAME" --all | jq -c --arg id "$1" '[.[] | select(.id == $id)] | first // empty'
}

sandbox_status() {
  local j
  j="$(sandbox_json "$1")" || return 1
  if [ -z "$j" ]; then echo missing; else jq -r .status <<<"$j"; fi
}

require_running_sandbox() {
  local id st
  id="$(sandbox_id)"
  st="$(sandbox_status "$id")" || die "could not query sandbox $id"
  [ "$st" = running ] || die "sandbox $id is $st, not running"
  printf '%s\n' "$id"
}

image_record_dir() { printf '%s\n' "$LOCAL_STATE/images"; }

git_tag() {
  git -C "$REPO_ROOT" rev-parse --short=12 HEAD 2>/dev/null || echo dev
}
