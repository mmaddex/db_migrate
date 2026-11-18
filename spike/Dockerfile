# Phase 0 spike image: minimal Debian, same base digest as the real image.
# Answers the platform questions in docs/spike.md (run spike/run_spike.sh).
ARG BASE_IMAGE=
FROM ${BASE_IMAGE:-debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2}

RUN apt-get update \
 && apt-get install -y --no-install-recommends procps ca-certificates curl util-linux \
 && rm -rf /var/lib/apt/lists/*

# Does the sandbox apply image ENV / ENTRYPOINT / CMD? The spike checks for
# this marker in `env` and for the file the entrypoint would write.
ENV SPIKE_DOCKERFILE_ENV=present
RUN printf '#!/bin/sh\ntouch /tmp/spike-entrypoint-ran\nexec "$@"\n' >/usr/local/bin/spike-entrypoint \
 && chmod 0755 /usr/local/bin/spike-entrypoint
ENTRYPOINT ["/usr/local/bin/spike-entrypoint"]
CMD ["sleep", "infinity"]
