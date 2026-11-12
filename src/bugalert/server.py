"""Drain application-owned streams before Uvicorn waits for HTTP requests."""

import socket
from collections.abc import Callable

import uvicorn


class MonitorServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, *, before_shutdown: Callable[[], None]):
        super().__init__(config)
        self._before_shutdown = before_shutdown

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        # ASGI lifespan shutdown runs AFTER Uvicorn drains connections. Infinite
        # SSE responses must therefore be stopped here, not only in lifespan.
        try:
            self._before_shutdown()
        finally:
            await super().shutdown(sockets=sockets)
