#!/usr/bin/env bash
# Build the migration image inside a Render sandbox started from Render's
# default image, and push it to the registry.
#
# The web app uploads the build context (image/, sandbox/, builder/ and
# .dockerignore) and runs this script detached. It needs root (or
# passwordless sudo), bash, tar, curl or wget, and a CA bundle. It downloads a
# pinned BuildKit release, so nothing has to be installed in the default image.
# Every request is HTTPS, so the build sandbox can run under an allow-list:
# github.com and release-assets.githubusercontent.com (BuildKit), the base
# image's registry (mirror.gcr.io by default), deb.debian.org and
# apt.postgresql.org (packages), and the registry pushed to.
#
# Registry credentials never enter the sandbox. The sandbox group has a
# brokered credential for the registry host that adds HTTP Basic auth to
# requests for <host>/token. The registry's normal token exchange therefore
# succeeds, and BuildKit pushes with the short-lived bearer token it gets back.
#
# Env:
#   IMAGE_REF           where to push, e.g. ghcr.io/acme/pg-migrate:3f2a9c1d (required)
#   CONTEXT_DIR         build context root (default: the directory above this script)
#   PG_MAJOR, PG_CLIENT_MAJORS, GIT_SHA, BASE_IMAGE
#                       build args for image/Dockerfile (PG_CLIENT_MAJORS is
#                       a space-separated list; unset keeps the Dockerfile's)
#   BUILDKIT_VERSION    default v0.35.0
#   BUILDKIT_SHA256     expected sha256 of the release tarball (optional; logged when unset)
#   BUILDKIT_BIN_DIR    use these binaries instead of downloading (tests)
#   REGISTRY_INSECURE   1 to push over plain HTTP (tests only)
#   WORK                scratch directory (default /tmp/pgm-buildkit)
#
# The last line on stdout is DIGEST=sha256:... on success.
set -euo pipefail

log() { printf '%s [build] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() { log "ERROR $*"; exit 1; }

: "${IMAGE_REF:?IMAGE_REF is required}"
CONTEXT_DIR="${CONTEXT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
BUILDKIT_VERSION="${BUILDKIT_VERSION:-v0.35.0}"
WORK="${WORK:-/tmp/pgm-buildkit}"
PG_MAJOR="${PG_MAJOR:-17}"
PG_CLIENT_MAJORS="${PG_CLIENT_MAJORS:-}"
GIT_SHA="${GIT_SHA:-unknown}"
BASE_IMAGE="${BASE_IMAGE:-}"

# --------------------------------------------------------- environment facts
# Recorded on every build: these answer the platform questions about the
# default sandbox image (docs/spike.md).
os="$( (. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-unknown}") || echo unknown)"
log "user=$(id -un 2>/dev/null || id -u) uid=$(id -u) arch=$(uname -m) kernel=$(uname -r) os='$os'"
log "pid1='$(tr '\0' ' ' </proc/1/cmdline 2>/dev/null | cut -c1-80)' cpus=$(nproc 2>/dev/null || echo ?) mem=$(awk '/MemTotal/ {print $2" kB"}' /proc/meminfo 2>/dev/null || echo ?)"
log "SSL_CERT_FILE=${SSL_CERT_FILE:-unset} SSL_CERT_DIR=${SSL_CERT_DIR:-unset}"

SUDO=()
if [ "$(id -u)" != 0 ]; then
  if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    SUDO=(sudo -n "--preserve-env=SSL_CERT_FILE,SSL_CERT_DIR")
    log "not root; using passwordless sudo"
  else
    die "buildkitd needs root or passwordless sudo, and this sandbox has neither"
  fi
fi

case "$(uname -m)" in
  x86_64|amd64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) die "unsupported architecture $(uname -m)" ;;
esac

mkdir -p "$WORK"
fetch() { # fetch URL FILE
  if command -v curl >/dev/null 2>&1; then curl -fsSL --retry 3 -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then wget -q -O "$2" "$1"
  else die "neither curl nor wget is available"
  fi
}
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

# ------------------------------------------------------------------- BuildKit
if [ -n "${BUILDKIT_BIN_DIR:-}" ]; then
  BIN="$BUILDKIT_BIN_DIR"
  log "using BuildKit binaries from $BIN"
else
  tarball="$WORK/buildkit-$BUILDKIT_VERSION.tgz"
  url="https://github.com/moby/buildkit/releases/download/$BUILDKIT_VERSION/buildkit-$BUILDKIT_VERSION.linux-$ARCH.tar.gz"
  if [ ! -s "$tarball" ]; then
    log "downloading BuildKit $BUILDKIT_VERSION ($ARCH)"
    fetch "$url" "$tarball.part" && mv "$tarball.part" "$tarball"
  fi
  got="$(sha256_of "$tarball")"
  if [ -n "${BUILDKIT_SHA256:-}" ]; then
    [ "$got" = "$BUILDKIT_SHA256" ] || die "BuildKit tarball sha256 $got does not match BUILDKIT_SHA256"
    log "BuildKit tarball sha256 verified"
  else
    log "BuildKit tarball sha256 $got (set BUILDKIT_SHA256 to pin it)"
  fi
  rm -rf "$WORK/buildkit" && mkdir -p "$WORK/buildkit"
  tar -xzf "$tarball" -C "$WORK/buildkit"
  BIN="$WORK/buildkit/bin"
fi
[ -x "$BIN/buildkitd" ] && [ -x "$BIN/buildctl" ] || die "buildkitd/buildctl not found in $BIN"
if [ -x "$BIN/buildkit-runc" ]; then RUNC="$BIN/buildkit-runc"
elif command -v runc >/dev/null 2>&1; then RUNC="$(command -v runc)"
else die "no runc: neither $BIN/buildkit-runc nor runc on PATH"
fi

# buildkitd only finds the OCI runtime as `runc` on PATH (its
# --oci-worker-binary flag does not satisfy that check), so expose it under
# that name. PATH is passed through env so sudo's secure_path can't drop it.
mkdir -p "$WORK/path"
ln -sf "$RUNC" "$WORK/path/runc"
RUN=("${SUDO[@]}" env "PATH=$WORK/path:$PATH")

SOCK="unix://$WORK/buildkitd.sock"
log "starting buildkitd (native snapshotter, runc $RUNC)"
# Native snapshotter: no overlayfs needed. Host networking for RUN steps.
"${RUN[@]}" "$BIN/buildkitd" --root "$WORK/root" --addr "$SOCK" \
  --oci-worker-snapshotter=native --oci-worker-net=host \
  --oci-worker-gc=false >"$WORK/buildkitd.log" 2>&1 &
BKPID=$!
cleanup() {
  "${SUDO[@]}" kill "$BKPID" 2>/dev/null || kill "$BKPID" 2>/dev/null || true
}
trap cleanup EXIT

for _ in $(seq 1 60); do
  if "${RUN[@]}" "$BIN/buildctl" --addr "$SOCK" debug workers >/dev/null 2>&1; then break; fi
  if ! kill -0 "$BKPID" 2>/dev/null; then
    tail -n 40 "$WORK/buildkitd.log" >&2 || true
    die "buildkitd exited during startup"
  fi
  sleep 1
done
"${RUN[@]}" "$BIN/buildctl" --addr "$SOCK" debug workers >/dev/null 2>&1 \
  || { tail -n 40 "$WORK/buildkitd.log" >&2; die "buildkitd did not become ready in 60s"; }

# ---------------------------------------------------------------------- build
output="type=image,name=$IMAGE_REF,push=true"
[ "${REGISTRY_INSECURE:-}" = 1 ] && output="$output,registry.insecure=true"
args=(--opt filename=Dockerfile --opt "build-arg:PG_MAJOR=$PG_MAJOR" --opt "build-arg:GIT_SHA=$GIT_SHA")
[ -n "$BASE_IMAGE" ] && args+=(--opt "build-arg:BASE_IMAGE=$BASE_IMAGE")
[ -n "$PG_CLIENT_MAJORS" ] && args+=(--opt "build-arg:PG_CLIENT_MAJORS=$PG_CLIENT_MAJORS")
# apt in the Dockerfile fetches over HTTPS with this sandbox's CA bundle (the
# base image has none), mounted as a build secret for the package step only.
CA_BUNDLE="${SSL_CERT_FILE:-/etc/ssl/certs/ca-certificates.crt}"
[ -s "$CA_BUNDLE" ] || die "no CA bundle at $CA_BUNDLE for the image's HTTPS package downloads (set SSL_CERT_FILE)"
args+=(--secret "id=ca,src=$CA_BUNDLE")

log "building $IMAGE_REF (PG_MAJOR=$PG_MAJOR, PG_CLIENT_MAJORS=${PG_CLIENT_MAJORS:-Dockerfile default}, git $GIT_SHA)"
start=$(date +%s)
"${RUN[@]}" "$BIN/buildctl" --addr "$SOCK" build --progress=plain \
  --frontend dockerfile.v0 \
  --local "context=$CONTEXT_DIR" --local "dockerfile=$CONTEXT_DIR/image" \
  "${args[@]}" \
  --output "$output" \
  --metadata-file "$WORK/metadata.json" \
  || die "build or push failed (if the push was denied: check the registry credential on the build sandbox group)"

digest="$(tr ',' '\n' <"$WORK/metadata.json" | sed -n 's/.*"containerimage.digest"[[:space:]]*:[[:space:]]*"\(sha256:[0-9a-f]*\)".*/\1/p' | head -n1)"
[ -n "$digest" ] || die "build finished but no image digest in metadata"
log "pushed $IMAGE_REF@$digest in $(( $(date +%s) - start ))s"
echo "DIGEST=$digest"
