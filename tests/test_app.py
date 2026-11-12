import copy
import json

import pytest
from fastapi.testclient import TestClient

from bugalert.app import create_app
from bugalert.catalog import advisory_url
from bugalert.feed import FeedError


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_health_endpoint_returns_application_status(write_config, method):
    app = create_app(write_config(), start_scheduler=False)
    with TestClient(app) as client:
        response = client.request(method, "/health")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        if method == "GET":
            assert response.json() == {"status": "ok"}
        else:
            assert response.content == b""
        assert "/health" not in app.openapi()["paths"]


@pytest.mark.parametrize("seed_file", ["seed.json", "missing.json"])
def test_health_is_independent_of_feed_errors_and_does_not_fetch(
    write_config, client_factory, seed_file
):
    upstream = client_factory(FeedError("Synthetic upstream outage"))
    app = create_app(write_config(seed_file=seed_file), client=upstream, start_scheduler=False)
    with TestClient(app) as client:
        refresh = client.post("/api/refresh", headers={"X-Bugalert-Request": "refresh"})
        assert refresh.json()["ok"] is False
        assert upstream.calls == 1
        for _ in range(3):
            response = client.get("/health")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
        assert upstream.calls == 1


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_health_reports_shutdown_without_caching(write_config, method):
    app = create_app(write_config(), start_scheduler=False)
    with TestClient(app) as client:
        app.state.monitor.begin_shutdown()
        response = client.request(method, "/health")
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        if method == "GET":
            assert response.json() == {"status": "shutting_down"}
        else:
            assert response.content == b""


def test_dashboard_filters_eos_and_returns_full_details(write_config):
    with TestClient(create_app(write_config(), start_scheduler=False)) as client:
        response = client.get("/api/dashboard")
        assert response.status_code == 200
        data = response.json()
        assert data["summary"] == {
            "total": 2,
            "bugs": 1,
            "security": 1,
            "severity_one": 1,
            "new": 0,
        }
        assert {alert["id"] for alert in data["alerts"]} == {100, 101}
        alert = client.get("/api/alerts/101").json()
        assert alert["affected_versions"] == ["4.32.1F"]
        assert alert["fixes_by_version"] == {"4.32.1F": "4.32.2"}
        assert alert["conditions"] == [["eapiEnabled", "hardwareFamily"]]
        assert alert["cves"] == ["CVE-2026-12345", "CVE-2026-54321"]
        assert alert["advisory_url"].startswith("https://www.arista.com/")
        assert client.get("/api/alerts/102").status_code == 404
        assert client.get("/api/alerts/99999").status_code == 404


@pytest.mark.parametrize(
    "path,content_type",
    [
        ("/", "text/html"),
        ("/static/styles.css", "text/css"),
        ("/static/app.js", "javascript"),
        ("/static/favicon.svg", "image/svg+xml"),
    ],
)
def test_frontend_assets_and_security_headers(write_config, path, content_type):
    with TestClient(create_app(write_config(), start_scheduler=False)) as client:
        response = client.get(path)
        assert response.status_code == 200
        assert content_type in response.headers["content-type"]
        assert "script-src 'self'" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert client.get("/bugalerts_json.py").status_code == 404
        assert client.get("/config.toml").status_code == 404


def test_manual_refresh_publishes_new_data(write_config, feed_payload, bug_factory, client_factory):
    updated = copy.deepcopy(feed_payload)
    updated["bugs"].append(bug_factory(999))
    upstream = client_factory(updated)
    with TestClient(create_app(write_config(), client=upstream, start_scheduler=False)) as client:
        response = client.post("/api/refresh", headers={"X-Bugalert-Request": "refresh"})
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert client.get("/api/dashboard").json()["summary"]["new"] == 1
        assert upstream.calls == 1


def test_refresh_refuses_cross_origin_and_simple_form_requests(
    write_config, client_factory, feed_payload
):
    upstream = client_factory(feed_payload)
    with TestClient(create_app(write_config(), client=upstream, start_scheduler=False)) as client:
        assert client.post("/api/refresh").status_code == 403
        response = client.post(
            "/api/refresh",
            headers={"X-Bugalert-Request": "refresh", "Origin": "https://malicious.example"},
        )
        assert response.status_code == 403
        assert upstream.calls == 0


def test_credentials_are_never_in_browser_payloads(write_config, monkeypatch):
    secret = "synthetic-secret-not-for-the-browser"
    monkeypatch.setenv("BUGALERT_ARISTA_TOKEN", secret)
    with TestClient(create_app(write_config(), start_scheduler=False)) as client:
        data = client.get("/api/dashboard").json()
        assert data["feed"]["token_configured"]
        assert secret not in json.dumps(data)
        assert secret not in client.get("/").text


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "https://malicious.example/advisory",
        "http://www.arista.com/advisory",
        "https://user:pass@www.arista.com/advisory",
        "https://www.arista.com:8080/advisory",
    ],
)
def test_untrusted_advisory_links_are_not_exposed(value):
    assert advisory_url(value) is None
