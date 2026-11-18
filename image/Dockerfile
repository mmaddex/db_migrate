#
# Migration runtime for Heroku -> Render Postgres with Bucardo.
#
# Contains: a local Postgres server (Bucardo's control database), plperl for
# it, the Postgres client tools, Bucardo 5.6.0 and the scripts in sandbox/
# (installed at /opt/migrate). No secrets: connection strings arrive at
# sandbox creation through --env-file.
#
# Nothing here relies on ENV, ENTRYPOINT or CMD at runtime. Render sandboxes
# may ignore them, so every script sources /opt/migrate/env.sh instead.
#
# Build from the repository root:
#   docker buildx build --platform linux/amd64 -f image/Dockerfile \
#     --build-arg PG_MAJOR=17 --build-arg GIT_SHA=$(git rev-parse HEAD) .

# Base image, pinned by digest (multi-arch index of debian:trixie-slim).
# Override BASE_IMAGE to pull the same digest through a registry mirror.
ARG BASE_IMAGE=
FROM ${BASE_IMAGE:-debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2}

# Postgres major for server, plperl and client tools. Set it to the Render
# database's major version. It must be >= Heroku's major so pg_dump can read
# the source.
ARG PG_MAJOR=17
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

# PGDG over plain http is the upstream-recommended setup (the official
# postgres image does the same); packages are verified with the pinned key.
# The "${PG_MAJOR}" component restricts version-specific packages to one major.
RUN set -eux; \
    export DEBIAN_FRONTEND=noninteractive; \
    echo "deb [signed-by=/usr/share/keyrings/postgresql.asc] http://apt.postgresql.org/pub/repos/apt ${DEBIAN_CODENAME}-pgdg main ${PG_MAJOR}" \
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
      bucardo \
      libdbi-perl libdbd-pg-perl libdbix-safe-perl libboolean-perl \
      jq procps ca-certificates curl util-linux; \
    # Guard: exactly one Postgres major, and the Bucardo version we expect.
    others="$(dpkg-query -W -f '${db:Status-Abbrev} ${Package}\n' 'postgresql-[0-9]*' 'postgresql-client-[0-9]*' 'postgresql-plperl-[0-9]*' 2>/dev/null \
      | awk '$1 == "ii" {print $2}' | grep -v -- "-${PG_MAJOR}\$" || true)"; \
    if [ -n "$others" ]; then echo "unexpected Postgres packages: $others" >&2; exit 1; fi; \
    bucardo --version | grep -qx "bucardo version ${BUCARDO_VERSION}"; \
    "/usr/lib/postgresql/${PG_MAJOR}/bin/postgres" --version; \
    # Size: the JIT plugin is the only thing that links LLVM (~175 MB with
    # libz3). The control database runs with jit=off and the client tools
    # never use it, so drop the plugin and its libraries.
    rm -rf "/usr/lib/postgresql/${PG_MAJOR}/lib/llvmjit"* "/usr/lib/postgresql/${PG_MAJOR}/lib/bitcode" \
           /usr/lib/*-linux-gnu/libLLVM* /usr/lib/*-linux-gnu/libz3.so*; \
    ! ldd "/usr/lib/postgresql/${PG_MAJOR}/bin/postgres" "/usr/lib/postgresql/${PG_MAJOR}/bin/pg_dump" \
        "/usr/lib/postgresql/${PG_MAJOR}/lib/plperl.so" | grep -q 'not found'; \
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
      echo "BUCARDO_VERSION=${BUCARDO_VERSION}"; \
      echo "IMAGE_REVISION=${GIT_SHA}"; } > /opt/migrate/image.env; \
    chmod 0755 /opt/migrate/*.sh; \
    chmod 0644 /opt/migrate/env.sh /opt/migrate/lib.sh /opt/migrate/image.env; \
    bash -n /opt/migrate/*.sh

# Convenience for `docker run` and the test harness only; sandboxes may
# ignore these, and no script depends on them.
WORKDIR /var/lib/migrate
CMD ["sleep", "infinity"]
