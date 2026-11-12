# BugAlert agent notes

## Development

Requires Python 3.14+ and uv. Run from the repository root:

```sh
uv sync --group dev
uv run bugalert --config config.toml
uv run pytest
uv run pytest tests/test_versions.py::test_fix_in_one_train_does_not_clear_a_sibling_train
uv run ruff check src tests
node --check src/bugalert/static/app.js  # optional; Node is not needed to run the app
```

- Unit tests use temporary state and mocked upstream responses; no Arista token is needed.
  `tests/conftest.py` automatically removes `BUGALERT_ARISTA_TOKEN` from the test environment.
- API tests use `create_app(write_config(), start_scheduler=False)` inside a `TestClient`
  context; inject `client_factory` responses rather than enabling real upstream requests.
- `tests/test_running_container.py` skips unless `BUGALERT_TEST_BASE_URL` is set. Its assertions
  expect the supplied container configuration, not an arbitrary local development server.

## Execution model and invariants

- `cli.main` wires `app.create_app` to `MonitorService`: one shared poller, SQLite cache,
  catalog, and SSE revision stream. Keep a single application worker.
- Launch through `bugalert`, not a replacement raw Uvicorn command. `server.MonitorServer`
  wakes SSE streams **before** Uvicorn drains connections; lifespan cleanup alone is too late.
  Verify shutdown changes with `uv run pytest tests/test_shutdown.py tests/test_service.py`.
- The frontend is packaged plain HTML/CSS/JS in `src/bugalert/static/`, served directly by
  FastAPI. There is no frontend build. The CSP disallows inline scripts and styles.
- `versions.ReleaseGraph` matches directed lineage, not numeric introduced/fixed intervals.
  Only ordinary F/M suffixes normalize away; FX/hardware branches remain distinct.
  `nofixyet` is not a fix; missing/disconnected releases have **unevaluated/null**, not zero, counts.
- `storage.StateStore` records all EOS IDs, independent of the watchlist. The first import is
  a non-new baseline; content changes and watchlist additions must not mark existing bugs new.
  Do not delete `.bugalert/state.sqlite3` during routine work: that resets discovery history.

## Configuration and feed handling

- Config selection is `--config`, then `BUGALERT_CONFIG`, then `config.toml` in the working
  directory. Seed/database paths resolve relative to the TOML file, not the working directory.
- Valid TOML edits reload every two seconds; invalid edits retain the last valid settings.
  Changing `feed.database_file` requires a restart.
- A valid SQLite snapshot takes precedence over the seed. `bugalerts.json` is a legacy
  Python-style dump, not strict JSON: preserve it unchanged. Only `feed.load_seed` enables
  newline repair and `ast.literal_eval`; remote downloads and cached feeds require validated strict JSON.
- Credentials come from the environment (`BUGALERT_ARISTA_TOKEN` by default), never TOML or
  browser payloads. The ignored local `bugalerts_json.py` contains a hard-coded credential;
  do not run, print, or commit it. Use `uv run bugalert-fetch --output /path/to/new-feed.json`.

## Containers

- Follow the exact Compose sequence in `README.md` under “Compose and CI tests” or
  `.github/workflows/test.yaml`: build `bugalert tests`, start `bugalert` with `up --wait`,
  then `docker compose --progress plain run --rm --no-deps -T tests`. `up --wait` alone does
  not run/report the suite. Compose publishes no host ports and uses a separate test-state volume.
- `docker/config.toml` is separate from local `config.toml`. Mount its **directory** at
  `/etc/bugalert` so atomic editor saves remain visible. Runtime is read-only, UID/GID 10001;
  persistent SQLite/WAL/SHM files belong in writable `/var/lib/bugalert`.
- `.dockerignore` is a build-input allowlist; new build inputs need explicit inclusion.
  Keep dev dependencies in the separate `test` build branch, not the final production runtime.
