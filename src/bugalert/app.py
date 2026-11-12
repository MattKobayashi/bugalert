"""FastAPI application and the server-sent update stream."""

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from bugalert.config import load_settings
from bugalert.feed import AristaClient
from bugalert.service import MonitorService

STATIC_DIR = Path(__file__).parent / "static"


async def event_stream(service: MonitorService, heartbeat_seconds: float = 15):
    async with service.subscribe() as queue:
        revision = service.revision
        while not service.stopping:
            data = json.dumps({"revision": revision})
            yield f"id: {revision}\nevent: update\ndata: {data}\n\n"
            while True:
                try:
                    revision = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
                    if revision is None:
                        return
                    break
                except TimeoutError:
                    yield ": keepalive\n\n"


def create_app(
    config_path: str | Path = "config.toml",
    *,
    client: AristaClient | None = None,
    start_scheduler: bool = True,
) -> FastAPI:
    settings = load_settings(config_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        service = MonitorService(settings, client=client)
        await service.start()
        app.state.monitor = service
        task = asyncio.create_task(service.run()) if start_scheduler else None
        try:
            yield
        finally:
            service.begin_shutdown()
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(
        title="Bugalert · EOS release intelligence",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/health", include_in_schema=False)
    @app.head("/health", include_in_schema=False)
    async def health(request: Request):
        # Probe the application, not Arista: missing/stale feed data must not
        # trigger restarts or cause each health check to fetch the upstream feed.
        stopping = request.app.state.monitor.stopping
        return JSONResponse(
            {"status": "shutting_down" if stopping else "ok"},
            status_code=503 if stopping else 200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/dashboard")
    async def dashboard(request: Request):
        return request.app.state.monitor.dashboard()

    @app.get("/api/alerts/{bug_id}")
    async def detail(bug_id: int, request: Request):
        alert = request.app.state.monitor.catalog.details.get(bug_id)
        if alert is None:
            raise HTTPException(status_code=404, detail="EOS alert not found in the current feed.")
        return alert

    @app.post("/api/refresh")
    async def refresh(request: Request):
        # A custom header prevents cross-site HTML forms from triggering requests
        # with the server's credential. There is deliberately no permissive CORS.
        if request.headers.get("X-Bugalert-Request") != "refresh":
            raise HTTPException(
                status_code=403, detail="A same-origin refresh request is required."
            )
        origin = request.headers.get("origin")
        if origin and (
            urlsplit(origin).netloc != request.url.netloc
            or urlsplit(origin).scheme != request.url.scheme
        ):
            raise HTTPException(
                status_code=403, detail="Cross-origin refresh requests are refused."
            )
        result = await request.app.state.monitor.refresh()
        return JSONResponse(
            {"ok": result, "refreshing": result is None}, status_code=202 if result is None else 200
        )

    @app.get("/api/events")
    async def events(request: Request):
        service: MonitorService = request.app.state.monitor

        return StreamingResponse(
            event_stream(service),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
