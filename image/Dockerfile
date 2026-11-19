#
# Migration runtime for Heroku -> Render Postgres with Bucardo.
#
# Contains: a local Postgres server (Bucardo's control database), plperl for
# it, the Postgres client tools for every major in PG_CLIENT_MAJORS, Bucardo
# 5.6.0 and the scripts in sandbox/ (installed at /opt/migrate). No secrets:
# connection strings arrive at sandbox creation through --env-file.
#
# Nothing here relies on ENV, ENTRYPOINT or CMD at runtime. Render sandboxes
# may ignore them, so every script sources /opt/migrate/env.sh instead.
#
# Build from the repository root:
#   docker buildx build --platform linux/amd64 -f image/Dockerfile \
#     --secret id=ca,src=/etc/ssl/certs/ca-certificates.crt \
#     --build-arg GIT_SHA=$(git rev-parse HEAD) .
#
# Every download is HTTPS, so the build works under a sandbox network
# allow-list (Render's allow-lists admit HTTPS only). The base image has no CA
# certificates, so apt uses the builder's bundle, mounted as the "ca" secret
# for the package step only (world-readable: apt downloads as _apt); it isn't kept in the image.

# Base image, pinned by digest (multi-arch index of debian:trixie-slim).
# Override BASE_IMAGE to pull the same digest through a registry mirror.
ARG BASE_IMAGE=
FROM ${BASE_IMAGE:-debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2}

# Postgres major of the local server (and plperl) that holds Bucardo's
# control database. It never talks to the migrated databases, so it doesn't
# have to match either of them.
ARG PG_MAJOR=17
# Majors whose client tools (pg_dump, psql) are installed side by side under
# /usr/lib/postgresql/<major>/bin. The scripts choose pg_dump per migration
# from the two servers' versions (pick_client_major in sandbox/lib.sh), so
# these are the source and target versions a migration can have.
ARG PG_CLIENT_MAJORS="14 15 16 17 18"
ARG BUCARDO_VERSION=5.6.0
ARG DEBIAN_CODENAME=trixie
ARG GIT_SHA=unknown

LABEL org.opencontainers.image.title="pg-migrate" \
      org.opencontainers.image.description="Bucardo-based Heroku to Render Postgres migration runtime" \
      org.opencontainers.image.revision="${GIT_SHA}"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Fixed uid/gid for the postgres user so file ownership is predictable.
# Postgres refuses to run as root; the scripts drop to this user when the
# sandbox shell is root (see env.sh).
RUN groupadd --system --gid 999 postgres \
 && useradd --system --uid 999 --gid postgres --home-dir /var/lib/postgresql \
      --shell /bin/bash postgres \
 && mkdir -p /var/lib/postgresql \
 && chown postgres:postgres /var/lib/postgresql

COPY image/keys/postgresql.asc /usr/share/keyrings/postgresql.asc

# Debian and PGDG over HTTPS (packages are also verified with the archive
# keys and the pinned PGDG key). PGDG's main component has every supported
# major's server and client packages; the "${PG_MAJOR}" component adds that
# major's builds of the shared libraries.
RUN --mount=type=secret,id=ca,target=/run/build-ca.crt,required=true,mode=0444 \
    set -eux; \
    export DEBIAN_FRONTEND=noninteractive; \
    clients=(); \
    for v in ${PG_CLIENT_MAJORS}; do \
      case "$v" in ''|*[!0-9]*) echo "PG_CLIENT_MAJORS: '$v' is not a major version" >&2; exit 1 ;; esac; \
      clients+=("postgresql-client-$v"); \
    done; \
    [ ${#clients[@]} -gt 0 ]; \
    echo 'Acquire::https::CAInfo "/run/build-ca.crt";' > /etc/apt/apt.conf.d/99build-ca; \
    sed -i 's|http://deb.debian.org/|https://deb.debian.org/|g' /etc/apt/sources.list.d/debian.sources; \
    ! grep -rq 'http://' /etc/apt/sources.list.d/; \
    echo "deb [signed-by=/usr/share/keyrings/postgresql.asc] https://apt.postgresql.org/pub/repos/apt ${DEBIAN_CODENAME}-pgdg main ${PG_MAJOR}" \
      > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends postgresql-common; \
    # Never create a default cluster; start_local.sh creates its own.
    sed -ri 's/#?\s*(create_main_cluster)\s*=.*/\1 = false/' /etc/postgresql-common/createcluster.conf; \
    grep -q '^create_main_cluster = false' /etc/postgresql-common/createcluster.conf; \
    apt-get install -y --no-install-recommends \
      "postgresql-${PG_MAJOR}" \
      "postgresql-plperl-${PG_MAJOR}" \
      "postgresql-client-${PG_MAJOR}" \
      "${clients[@]}" \
      bucardo \
      libdbi-perl libdbd-pg-perl libdbix-safe-perl libboolean-perl \
      jq procps ca-certificates curl util-linux; \
    # Guard: one server major, exactly the client majors asked for, and the
    # Bucardo version we expect.
    installed="$(dpkg-query -W -f '${db:Status-Abbrev} ${Package}\n' 'postgresql-[0-9]*' 'postgresql-client-[0-9]*' 'postgresql-plperl-[0-9]*' 2>/dev/null \
      | awk '$1 == "ii" {print $2}' | sort || true)"; \
    expected="$(printf '%s\n' "postgresql-${PG_MAJOR}" "postgresql-plperl-${PG_MAJOR}" "postgresql-client-${PG_MAJOR}" "${clients[@]}" | sort -u)"; \
    if [ "$installed" != "$expected" ]; then \
      echo "unexpected Postgres packages:" >&2; diff <(echo "$expected") <(echo "$installed") >&2 || true; exit 1; \
    fi; \
    bucardo --version | grep -qx "bucardo version ${BUCARDO_VERSION}"; \
    "/usr/lib/postgresql/${PG_MAJOR}/bin/postgres" --version; \
    for v in ${PG_CLIENT_MAJORS}; do \
      "/usr/lib/postgresql/$v/bin/pg_dump" --version | grep -q " $v\."; \
      ! ldd "/usr/lib/postgresql/$v/bin/pg_dump" "/usr/lib/postgresql/$v/bin/psql" | grep -q 'not found'; \
    done; \
    # Size: the JIT plugin is the only thing that links LLVM (~175 MB with
    # libz3). The control database runs with jit=off and the client tools
    # never use it, so drop the plugin and its libraries.
    rm -rf "/usr/lib/postgresql/${PG_MAJOR}/lib/llvmjit"* "/usr/lib/postgresql/${PG_MAJOR}/lib/bitcode" \
           /usr/lib/*-linux-gnu/libLLVM* /usr/lib/*-linux-gnu/libz3.so*; \
    ! ldd "/usr/lib/postgresql/${PG_MAJOR}/bin/postgres" "/usr/lib/postgresql/${PG_MAJOR}/bin/pg_dump" \
        "/usr/lib/postgresql/${PG_MAJOR}/lib/plperl.so" | grep -q 'not found'; \
    # The build's CA bundle is gone after this step; the image has its own now.
    rm /etc/apt/apt.conf.d/99build-ca; \
    # Size: drop docs, man pages and apt lists.
    apt-get clean; \
    rm -rf /var/lib/apt/lists/* /usr/share/doc/* /usr/share/man/* /usr/share/info/* \
           /usr/share/locale/*/LC_MESSAGES/*.mo /var/cache/debconf/*-old

# Bucardo config: local control database over the unix socket, plus the PID
# and log directories. Work directory holds artifacts, logs and state.
COPY image/bucardorc /etc/bucardorc
RUN set -eux; \
    chmod 0644 /etc/bucardorc; \
    install -d -o postgres -g postgres -m 0755 /var/run/bucardo /var/lib/migrate \
      /var/lib/migrate/logs /var/lib/migrate/artifacts /var/lib/migrate/state /var/lib/migrate/jobs; \
    install -d -o postgres -g postgres -m 2775 /var/run/postgresql; \
    install -d -o postgres -g postgres -m 0700 /var/lib/postgresql/.pg-migrate

COPY sandbox/ /opt/migrate/
RUN set -eux; \
    { echo "PG_MAJOR=${PG_MAJOR}"; \
      echo "PG_CLIENT_MAJORS='${PG_CLIENT_MAJORS}'"; \
      echo "BUCARDO_VERSION=${BUCARDO_VERSION}"; \
      echo "IMAGE_REVISION=${GIT_SHA}"; } > /opt/migrate/image.env; \
    chmod 0755 /opt/migrate/*.sh; \
    chmod 0644 /opt/migrate/env.sh /opt/migrate/lib.sh /opt/migrate/image.env; \
    bash -n /opt/migrate/*.sh

# Convenience for `docker run` and the test harness only; sandboxes may
# ignore these, and no script depends on them.
WORKDIR /var/lib/migrate
CMD ["sleep", "infinity"]
