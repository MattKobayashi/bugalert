# BugAlert

A local-first web dashboard for **Arista EOS bugs and security advisories**, matched to the releases you run. The server checks Arista on a schedule and pushes updates to every open browser. No frontend build step, Node.js, or external database is required.

## Run

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run bugalert
```

Open **http://127.0.0.1:8000**. Without a token, the application starts with the supplied `bugalerts.json` and clearly labels it as a **local snapshot**, not live data.

To enable live checks, set your Arista feed token **before** starting the server:

```sh
export BUGALERT_ARISTA_TOKEN='your-arista-feed-token'
uv run bugalert --config config.toml
```

Restart a running server after changing its environment. The token is base64-encoded and posted using the supplied script's `token_auth` / `file_version = "2"` protocol. It is never returned by the web API, placed in TOML, or stored in the cache.

## Choose your EOS versions

Edit the external [`config.toml`](config.toml). The included versions are **examples**, not an inventory discovered from your network.

```toml
[eos]
versions = ["4.32.8M", "4.33.5M", "4.34.2F"]

[feed]
refresh_seconds = 900
timeout_seconds = 30
new_window_hours = 72
token_env = "BUGALERT_ARISTA_TOKEN"
seed_file = "bugalerts.json"
database_file = ".bugalert/state.sqlite3"
```

- **Live config reload:** valid version and interval changes appear within two seconds, without a restart. Invalid TOML or settings show an error while the last valid configuration remains active.
- **Check interval:** defaults to 15 minutes; allowed range is 30 seconds to 24 hours. Checks run on the server even when no browser is open. A manual check does not overlap an already running check.
- **Paths:** relative to the TOML file, not the current working directory. The database directory must be writable. Changing `database_file` requires a restart. `seed_file` is a bootstrap source, used only if there is no valid cached feed.
- **Other config locations:** `uv run bugalert --config /path/to/network.toml`, or set `BUGALERT_CONFIG`.
- **Bind address / port:** `uv run bugalert --host 127.0.0.1 --port 8080`.

## Docker

The multi-stage [`Dockerfile`](Dockerfile) uses digest-pinned Python 3.14 slim and uv images, installs only locked production dependencies, and runs as **UID/GID 10001**. uv and build sources stay out of the runtime image. [`.dockerignore`](.dockerignore) allowlists build inputs so the legacy credential-bearing script, `.env` files, your local TOML, and live SQLite state never enter the build context.

### Build and run

```sh
docker build --pull --tag bugalert:local .
```

Edit [`docker/config.toml`](docker/config.toml) to set your releases and interval, or copy its layout into a dedicated configuration directory. **Your existing local `config.toml` is unchanged.** The container example uses absolute paths for the bundled seed and the persistent database; copy your watchlist/settings into it rather than reusing local relative data paths unchanged.

For live checks, export the token in the shell that runs Docker. Omit the token to use the bundled seed snapshot instead.

```sh
export BUGALERT_ARISTA_TOKEN='your-arista-feed-token'

docker run --detach \
  --name bugalert \
  --init \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --stop-timeout 45 \
  --publish 127.0.0.1:8080:8080 \
  --mount "type=bind,source=$(pwd)/docker,target=/etc/bugalert,readonly" \
  --mount type=volume,source=bugalert-data,target=/var/lib/bugalert \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --env BUGALERT_ARISTA_TOKEN \
  bugalert:local
```

Open **http://127.0.0.1:8080**. Publishing to host loopback keeps this unauthenticated application private; use an authenticated TLS reverse proxy when sharing it. The application listens on all interfaces **inside** the container so Docker can forward requests. To use a different host port, change only the host side of the publish mapping, keeping the container port at 8080 for its health check.

### Configuration, state, and operations

- **Configuration:** `/etc/bugalert/config.toml`, selected by `BUGALERT_CONFIG`. Mount a dedicated **directory**, not just an individual file: this lets hot reload see editor saves that atomically replace the TOML file. Ensure the directory and file are readable by UID 10001. Valid watchlist and interval changes still reload without restarting or rebuilding the image.
- **Persistence:** the named `bugalert-data` volume stores `/var/lib/bugalert/state.sqlite3` and its SQLite WAL/SHM files. Docker initializes a new empty volume with the image directory's ownership. Reuse this volume when recreating the container to preserve the downloaded feed and discovery history. If using a host bind mount instead, make it writable by UID/GID 10001. The image's `VOLUME` declaration creates an anonymous volume if no explicit mount is supplied; use the named volume shown above for predictable persistence.
- **Credentials:** the token is supplied only at runtime, never through a build argument or image layer. `--env BUGALERT_ARISTA_TOKEN` copies the exported value without putting the literal token in the Docker command. Runtime environment values remain visible to users with Docker administration access; use your deployment platform's secret injection facilities when available. Changing the token requires **recreating** the container with the updated environment, not merely `docker restart`.
- **Health:** Docker probes `http://127.0.0.1:8080/health` using Python's standard library; no curl or extra system package is needed. This is application liveness, not confirmation of a fresh feed or valid token. Inspect feed status in the dashboard.
- **Shutdown:** the exec-form entrypoint runs Bugalert's shutdown-aware server. Docker's SIGTERM closes event streams and stops the poller. The 45-second stop allowance accommodates the default upstream request timeout if a download is in flight; adjust it if you increase `feed.timeout_seconds`.
- **Logs:** `docker logs --follow bugalert`. Stop with `docker stop bugalert`. Do not remove the data volume when replacing the container unless intentionally resetting the discovery baseline.
- **Updates:** digest pins make base/tool image selection reproducible, but must be refreshed deliberately to receive Python/OS/uv security fixes. Dependency upgrades require updating `uv.lock`, then rebuilding and recreating the container. The supplied seed is only a bootstrap snapshot, not a substitute for live checks.

### Compose and CI tests

[`compose.yaml`](compose.yaml) defines an isolated test stack: the production `bugalert` service and a separate `tests` runner built with the locked development dependencies. The normal/default Docker image remains production-only. The stack publishes no host ports and uses its own project-scoped SQLite volume, not the standalone production `bugalert-data` volume.

Run the same sequence as [the GitHub Actions workflow](.github/workflows/test.yaml):

```sh
export COMPOSE_PROJECT_NAME=bugalert-ci
docker compose --progress plain build --pull bugalert tests
docker compose --progress plain up --wait --wait-timeout 60 bugalert
docker compose --progress plain run --rm --no-deps -T tests
docker compose down --volumes --remove-orphans
```

The application must pass its image health check before testing. The test runner executes the complete pytest suite, including read-only HTTP smoke tests against the running production container: health, packaged frontend assets, loaded watchlist/feed, live-update streaming, and rejection of access to private files. Its exit status becomes the workflow result. A plain `up --wait` is **not** sufficient to wait for a one-shot test suite to finish or report its failures.

The workflow always tears down the test stack/volume and prints application logs on failure. The Arista token is optional, is passed only to the application at runtime, and is never passed to the test runner or build. Fork pull requests work with the bundled seed, and the tests do not require successful upstream access. The `tests` service has a profile so an ordinary `docker compose up` does not accidentally start the test suite; explicitly targeting it enables it automatically. The container TOML example is mounted read-only for tests rather than being copied into either image. Host `uv run pytest` skips the running-container smoke tests unless `BUGALERT_TEST_BASE_URL` is set.

## Dashboard

- Search summaries, bug IDs, CVEs, and affected monitored releases.
- Filter bugs versus security advisories, Arista severity, monitored release, and newly discovered alerts.
- Sort by export date, severity, or first observed date; browse all results with pagination.
- Open full notes, affected versions, same-train fix boundaries, CVE references, Arista advisory links, and hardware / enabled-feature conditions.
- Export **all results in the current filtered view**, not just the current page, as CSV.
- Follow an alert directly using `/?alert=2048764` or copy its link from the details panel.
- Browser updates use server-sent events, with reconnect and a 30-second polling fallback. Upstream failures leave the last valid feed visible, along with its status and last successful check time.

Counts represent unique feed bug IDs, not unique CVEs or advisory URLs. One vulnerability or advisory may cover multiple bug entries.

### How matching works

Matching uses the feed's **directed `releaseGraph`**, not a numeric `introduced <= version < fixed` comparison. Starting at a monitored release, it walks back through the release lineage, stopping at fixes and looking for introductions. This respects sibling branches and bugs reintroduced after an earlier fix.

- Standard **F/M** suffixes normalize to the graph's numeric release names.
- **FX / hardware-specific branches remain distinct**; a special build's fix is not suggested as a standard-build fix.
- A `nofixyet` marker never counts as a fix.
- A missing release, or one with a disconnected lineage, is explicitly **unevaluated** and has no numeric alert count. It is never silently treated as unaffected.
- “Same-train fix” is the earliest listed reachable fix in that release train, not a blanket upgrade recommendation.
- Security entries have a non-empty `cve` or `securityAdvisoryURL`. Severity comes from Arista's `sev1` / `sev2` / `sev3`, **not CVSS**.

**This is version-based screening, not device-level confirmation.** The application does not know your switches' hardware or enabled features. Review the displayed conditions and the advisory before deciding whether an alert applies or selecting a remediation. The dashboard is only as complete and current as Arista's feed.

### What “new” means

The first imported feed establishes a baseline and is not marked new. Later, previously unseen **EOS bug IDs** are marked new for `new_window_hours` (72 by default). Observation history covers all EOS entries, not just the versions currently monitored, and survives restarts in SQLite. Adding a release to TOML does not turn existing feed entries into new discoveries. Content changes to an existing bug update its observed-change timestamp without creating a new ID.

The supplied dump is Python-style output rather than strict JSON and has physical line breaks inside quoted strings. The **local seed loader only** safely repairs these and uses `ast.literal_eval`, never `eval`. Remote downloads must be valid, schema-checked JSON. The original sample is never modified.

## Download a feed separately

```sh
uv run bugalert-fetch --output /path/to/new-feed.json
# Omit --output to write strict JSON to stdout.
```

This uses the same TOML / environment configuration and validated client as the server. It does not change the running monitor's cached snapshot.

## Deployment notes

Stop the server with **Ctrl+C**; open dashboard tabs do not need to be closed first. The `bugalert` entrypoint closes live-update streams before Uvicorn waits for active connections, then cancels and awaits the background poller during application cleanup. Merely closing streams in ASGI lifespan shutdown would be too late: Uvicorn drains HTTP connections before invoking that cleanup.

The default bind is **loopback only**. There is no built-in authentication. If sharing this dashboard, put it behind an authenticated TLS reverse proxy and restrict access; do not expose it directly to the public internet. Run a **single application worker** so there is one poller and one update stream. Disable proxy buffering on `/api/events` and allow long-lived event-stream connections. The app sends `X-Accel-Buffering: no` for compatible proxies.

Health probes can use **`GET /health`**. It returns HTTP **200** with `{"status":"ok"}` while the application is running, or **503** with `{"status":"shutting_down"}` during shutdown. `HEAD /health` returns the same status and headers without a body. Responses are not cached. This lightweight endpoint never fetches Arista or checks feed freshness; missing credentials, an upstream outage, or an unavailable seed do not make the application unhealthy. Feed status and errors remain available in the dashboard and `/api/dashboard`.

Only packaged frontend assets are served; the TOML file, legacy script, raw seed, and state database are not web-accessible. Feed text is rendered as text, advisory links are restricted to HTTPS Arista URLs, refresh requests require a same-origin custom header, and a restrictive Content Security Policy blocks inline scripts. CSV cells are protected against spreadsheet formula injection.

Back up `.bugalert/state.sqlite3` if preserving first-seen history is important. With the server stopped, removing this application-owned database resets the cache and discovery baseline. Do not remove it during normal operation.

## Development

```sh
uv sync --group dev
uv run pytest
uv run ruff check src tests
node --check src/bugalert/static/app.js  # optional JS syntax check
```

Unit tests use local fixtures and mocked HTTP responses; they do not contact Arista or need a real token. Compose additionally runs smoke tests against its production container; those checks also work without a token. The app is Python / FastAPI with plain, locally served HTML, CSS, and JavaScript. All runtime state is in SQLite through Python's standard library.
