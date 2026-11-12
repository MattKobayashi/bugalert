# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e

# Keep builder and runtime on the same Python/OS image so the copied virtual
# environment uses the same interpreter and native-library ABI. Refresh these
# registry-verified manifest digests deliberately when applying security updates.
FROM python:3.15-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS python-base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv

FROM python-base AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_PYTHON_DOWNLOADS=0 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /opt/bugalert

# Cache locked production dependencies independently of application changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# Development dependencies are installed only in this separate branch of the
# build graph. The production runtime still copies the original builder venv.
FROM builder AS test-dependencies

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --group dev --no-editable

FROM python-base AS test

RUN groupadd --gid 10001 bugalert \
    && useradd --no-log-init --uid 10001 --gid 10001 --no-create-home \
        --shell /usr/sbin/nologin bugalert \
    && install -d -m 0755 /opt/bugalert/docker

WORKDIR /opt/bugalert

COPY --from=test-dependencies /opt/bugalert/.venv /opt/bugalert/.venv
COPY pyproject.toml bugalerts.json ./
COPY tests/ ./tests/

ENV PATH="/opt/bugalert/.venv/bin:${PATH}"

USER 10001:10001

ENTRYPOINT ["/opt/bugalert/.venv/bin/python", "-m", "pytest"]
CMD ["-q", "-o", "cache_dir=/tmp/pytest-cache"]

# Keep production last so a plain `docker build .` still builds the runtime.
FROM python-base AS runtime

LABEL org.opencontainers.image.title="Bugalert" \
    org.opencontainers.image.description="Version-aware Arista EOS bug and security alert dashboard"

RUN groupadd --gid 10001 bugalert \
    && useradd --no-log-init --uid 10001 --gid 10001 --no-create-home \
        --shell /usr/sbin/nologin bugalert \
    && install -d -m 0755 /opt/bugalert /etc/bugalert \
    && install -d -o 10001 -g 10001 -m 0750 /var/lib/bugalert

WORKDIR /opt/bugalert

# Only the installed application and its production dependencies reach runtime:
# no uv, build sources, development dependencies, local config, or credentials.
COPY --from=builder /opt/bugalert/.venv /opt/bugalert/.venv
COPY --chmod=0444 bugalerts.json ./bugalerts.json

ENV PATH="/opt/bugalert/.venv/bin:${PATH}" \
    BUGALERT_CONFIG=/etc/bugalert/config.toml

USER 10001:10001

VOLUME ["/var/lib/bugalert"]
EXPOSE 8080/tcp
STOPSIGNAL SIGTERM

# Use the standard library instead of installing curl. http.client bypasses
# proxy environment variables, so this probe always checks this container.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import http.client; c = http.client.HTTPConnection('127.0.0.1', 8080, timeout=2); c.request('GET', '/health'); r = c.getresponse(); r.read(); c.close(); raise SystemExit(0 if r.status == 200 else 1)"]

# Run the application's shutdown-aware server directly, not uv or a shell.
ENTRYPOINT ["/opt/bugalert/.venv/bin/bugalert"]
CMD ["--host", "0.0.0.0", "--port", "8080"]
