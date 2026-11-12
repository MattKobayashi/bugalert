"""Read-only smoke tests against the production image started by Compose."""

import http.client
import json
import os
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest

BASE_URL = os.environ.get("BUGALERT_TEST_BASE_URL", "")
pytestmark = pytest.mark.skipif(
    not BASE_URL, reason="BUGALERT_TEST_BASE_URL is set by the Compose test service"
)


@contextmanager
def response_from(path: str, method: str = "GET"):
    url = urlsplit(BASE_URL)
    connection_class = {
        "http": http.client.HTTPConnection,
        "https": http.client.HTTPSConnection,
    }[url.scheme]
    connection = connection_class(url.hostname, url.port, timeout=5)
    try:
        # http.client deliberately ignores proxy environment variables for
        # traffic between services on the private Compose network.
        connection.request(method, url.path.rstrip("/") + path)
        response = connection.getresponse()
        try:
            yield response
        finally:
            response.close()
    finally:
        connection.close()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_running_health(method):
    with response_from("/health", method) as response:
        assert response.status == 200
        assert response.getheader("Cache-Control") == "no-store"
        body = response.read()
        if method == "GET":
            assert json.loads(body) == {"status": "ok"}
        else:
            assert body == b""


@pytest.mark.parametrize(
    "path,content_type",
    [
        ("/", "text/html"),
        ("/static/styles.css", "text/css"),
        ("/static/app.js", "javascript"),
        ("/static/favicon.svg", "image/svg+xml"),
    ],
)
def test_running_frontend(path, content_type):
    with response_from(path) as response:
        assert response.status == 200
        assert content_type in response.getheader("Content-Type", "")
        assert response.getheader("X-Content-Type-Options") == "nosniff"
        assert response.read()


def test_running_dashboard_has_loaded_the_feed_and_watchlist():
    with response_from("/api/dashboard") as response:
        assert response.status == 200
        data = json.loads(response.read())
    assert data["feed"]["generation_id"]
    assert data["feed"]["source"] in {"seed", "arista"}
    assert data["config"]["path"] == "/etc/bugalert/config.toml"
    assert data["config"]["versions"]
    assert data["summary"]["total"] == len(data["alerts"])
    assert len({alert["id"] for alert in data["alerts"]}) == len(data["alerts"])
    for alert in data["alerts"]:
        assert alert["affected_versions"]
        assert set(alert["affected_versions"]) <= set(data["config"]["versions"])


def test_running_event_stream_delivers_a_revision():
    with response_from("/api/events") as response:
        assert response.status == 200
        assert "text/event-stream" in response.getheader("Content-Type", "")
        assert response.getheader("X-Accel-Buffering") == "no"
        lines = []
        for _ in range(8):
            line = response.readline(8192)
            if line in {b"", b"\n", b"\r\n"}:
                break
            lines.append(line)
        assert b"event: update\n" in lines
        payload = next(line.removeprefix(b"data: ") for line in lines if line.startswith(b"data: "))
        assert isinstance(json.loads(payload)["revision"], int)


@pytest.mark.parametrize("path", ["/config.toml", "/bugalerts_json.py", "/bugalerts.json"])
def test_running_image_does_not_serve_private_files(path):
    with response_from(path) as response:
        assert response.status == 404


def test_running_refresh_rejects_requests_without_the_custom_header():
    # Rejected before contacting Arista, even when CI has a real feed token.
    with response_from("/api/refresh", method="POST") as response:
        assert response.status == 403
