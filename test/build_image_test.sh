#!/usr/bin/env bash
# Test builder/build_image.sh the way the web app runs it in a sandbox:
# a plain Debian container (standing in for Render's default sandbox image)
# with root, BuildKit binaries, and a local registry to push to.
#
#   test/build_image_test.sh
#
# Env: BUILDKIT_IMAGE (default moby/buildkit:v0.35.0), DEBIAN_IMAGE,
# BASE_IMAGE (build arg), CA_BUNDLE (CA file to give the container; default the host's).
set -euo pipefail
cd "$(dirname "$0")/.."
NET=pgm-build-test
BK_IMAGE="${BUILDKIT_IMAGE:-moby/buildkit:v0.35.0}"
DEB_IMAGE="${DEBIAN_IMAGE:-debian:trixie-slim}"
REG_IMAGE="${REGISTRY_IMAGE:-registry:2}"
WORK="$(mktemp -d)"
cleanup() {
  docker rm -f pgm-build-registry pgm-build-runner >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
cleanup; WORK="$(mktemp -d)"

docker network create "$NET" >/dev/null
docker run -d --name pgm-build-registry --network "$NET" -p 127.0.0.1:5055:5000 "$REG_IMAGE" >/dev/null

# BuildKit binaries from the official image (the sandbox downloads the release).
cid="$(docker create "$BK_IMAGE")"
mkdir -p "$WORK/bin"
for b in buildkitd buildctl buildkit-runc; do docker cp -q "$cid:/usr/bin/$b" "$WORK/bin/$b"; done
docker rm "$cid" >/dev/null

# The build context exactly as the web app uploads it.
mkdir -p "$WORK/ctx"
cp -R image sandbox builder .dockerignore "$WORK/ctx/"

# The plain Debian image has no CA certificates; Render's default image does.
CA_BUNDLE="${CA_BUNDLE:-/etc/ssl/certs/ca-certificates.crt}"
ca_args=(-v "$CA_BUNDLE:/etc/ssl/certs/ca-certificates.crt:ro")

start=$(date +%s)
docker run --name pgm-build-runner --network "$NET" --privileged \
  -v "$WORK/ctx:/tmp/pgm-build:ro" -v "$WORK/bin:/opt/bk:ro" ${ca_args[@]+"${ca_args[@]}"} \
  -e IMAGE_REF=pgm-build-registry:5000/pg-migrate:test -e REGISTRY_INSECURE=1 \
  -e BUILDKIT_BIN_DIR=/opt/bk -e GIT_SHA=test -e PG_MAJOR="${PG_MAJOR:-17}" \
  -e BASE_IMAGE="${BASE_IMAGE:-}" \
  "$DEB_IMAGE" bash /tmp/pgm-build/builder/build_image.sh | tee "$WORK/out.txt"
digest="$(sed -n 's/^DIGEST=//p' "$WORK/out.txt")"
[ -n "$digest" ] || { echo "FAIL: no digest"; exit 1; }

docker pull -q "127.0.0.1:5055/pg-migrate:test" >/dev/null
docker run --rm "127.0.0.1:5055/pg-migrate:test" bash -c 'cat /opt/migrate/image.env && bucardo --version && test -x /opt/migrate/preflight.sh'
# The control cluster comes with Bucardo installed, and with no password saved.
docker run --rm "127.0.0.1:5055/pg-migrate:test" bash -c '
  out="$(/opt/migrate/start_local.sh 2>&1)" || { echo "$out"; exit 1; }
  echo "$out"
  grep -q "Bucardo already installed" <<<"$out" || { echo "FAIL: start_local.sh had to install Bucardo"; exit 1; }
  ! test -e /var/lib/postgresql/.pgpass || { echo "FAIL: the image has a .pgpass"; exit 1; }'
docker rmi -f "127.0.0.1:5055/pg-migrate:test" >/dev/null 2>&1 || true
echo "BUILD TEST PASSED in $(( $(date +%s) - start ))s ($digest)"
