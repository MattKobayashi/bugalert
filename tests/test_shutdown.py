"""Exercise real terminal signals while browser-like SSE connections stay open."""

import asyncio
import os
import signal
import socket
import sys
from contextlib import suppress

import pytest


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminal signal semantics")
@pytest.mark.parametrize("exit_signal", [signal.SIGINT, signal.SIGTERM], ids=["ctrl-c", "sigterm"])
@pytest.mark.parametrize("connections", [0, 1, 3], ids=["no-tabs", "one-tab", "three-tabs"])
def test_cli_shuts_down_cleanly_with_open_event_streams(write_config, exit_signal, connections):
    path = write_config()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]

    async def scenario():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "bugalert",
            "--config",
            str(path),
            "--port",
            str(port),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "WEB_CONCURRENCY": "1"},
        )
        logs = []
        started = asyncio.Event()
        streams = []

        async def collect_logs():
            try:
                while line := await process.stdout.readline():
                    text = line.decode()
                    logs.append(text)
                    if "Uvicorn running on" in text:
                        started.set()
            finally:
                started.set()  # Also wake the test if startup failed.

        log_task = asyncio.create_task(collect_logs())
        try:
            await asyncio.wait_for(started.wait(), timeout=10)
            assert any("Uvicorn running on" in line for line in logs), "".join(logs)

            for _ in range(connections):
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                streams.append((reader, writer))
                writer.write(
                    f"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                    "Accept: text/event-stream\r\n\r\n".encode()
                )
                await writer.drain()
                headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
                assert headers.startswith(b"HTTP/1.1 200"), headers
                assert b"transfer-encoding: chunked" in headers.lower(), headers
                event = await asyncio.wait_for(reader.readuntil(b"\n\n"), timeout=5)
                assert b"event: update" in event

            # Do not close the clients before stopping the server: that would
            # conceal the shutdown deadlock this test is intended to reproduce.
            process.send_signal(exit_signal)
            await asyncio.wait_for(process.wait(), timeout=8)
            await asyncio.wait_for(log_task, timeout=2)
            output = "".join(logs)
            assert "timeout graceful shutdown exceeded" not in output, output
            assert "Exception in ASGI application" not in output, output
            assert "CancelledError" not in output, output
            assert "Task was destroyed" not in output, output
            assert "Application shutdown complete" in output, output
            assert process.returncode == (0 if exit_signal == signal.SIGINT else -signal.SIGTERM)

            for reader, _ in streams:
                tail = await asyncio.wait_for(reader.read(), timeout=2)
                # The response must finish normally, not just lose its socket.
                assert tail.endswith(b"0\r\n\r\n"), tail
        finally:
            for _, writer in streams:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()
            if process.returncode is None:
                process.kill()
                await process.wait()
            await log_task

    asyncio.run(scenario())
