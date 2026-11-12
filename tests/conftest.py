import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from bugalert.feed import validate_feed


def make_bug(bug_id=100, **overrides):
    return {
        "bugId": bug_id,
        "product": "eos",
        "severity": "sev2",
        "alertSummary": "An example forwarding issue",
        "alertNote": "The full note.\nWith another line.",
        "releaseNote": "The full note.\nWith another line.",
        "cve": "",
        "securityAdvisoryURL": "",
        "versionIntroduced": ["1.0.0"],
        "versionFixed": ["4.31.2"],
        "exportedTime": 1794268800,
        **overrides,
    }


@pytest.fixture(autouse=True)
def no_real_credentials(monkeypatch):
    monkeypatch.delenv("BUGALERT_ARISTA_TOKEN", raising=False)


@pytest.fixture
def bug_factory():
    return make_bug


@pytest.fixture
def feed_payload():
    return {
        "genId": "initial-generation",
        "releaseDate": "2026-11-10",
        "bugs": [
            make_bug(),
            make_bug(
                101,
                severity="sev1",
                cve="CVE-2026-12345, CVE-2026-54321",
                alertSummary="A security issue in eAPI",
                securityAdvisoryURL=(
                    "https: //www.arista.com/en/support/advisories-notices/security-advisory/123"
                ),
                versionFixed=["4.31.1", "4.32.2"],
                conjunction=[[{"tag": "eapiEnabled"}, {"tag": "hardwareFamily"}]],
            ),
            make_bug(102, product="terminattr"),
        ],
        "releaseGraph": [
            ["1.0.0", "4.30.0"],
            ["4.30.0", "4.30.1"],
            ["4.30.1", "4.30.2"],
            ["4.30.2", "4.30.3"],
            ["4.30.0", "4.31.0"],
            ["4.31.0", "4.31.1"],
            ["4.31.1", "4.31.2"],
            ["4.31.2", "4.31.3"],
            ["4.31.0", "4.32.0"],
            ["4.32.0", "4.32.1"],
            ["4.32.1", "4.32.2"],
            ["4.31.1", "4.31.1FX-Test"],
            ["4.31.1FX-Test", "4.31.1FX-Test.2"],
        ],
        "releases": [
            {"release": "4.31.1", "product": "eos", "type": "maintenance"},
            {"release": "4.32.1", "product": "eos", "type": "feature"},
        ],
    }


@pytest.fixture
def write_config(tmp_path, feed_payload):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(feed_payload), encoding="utf-8")
    config = tmp_path / "config.toml"

    def write(versions=None, **feed):
        versions = ["4.31.1M", "4.32.1F"] if versions is None else versions
        settings = {
            "refresh_seconds": 30,
            "seed_file": "seed.json",
            "database_file": "state.sqlite3",
            **feed,
        }
        config.write_text(
            f"[eos]\nversions = {json.dumps(versions)}\n\n[feed]\n"
            + "".join(f"{key} = {json.dumps(value)}\n" for key, value in settings.items()),
            encoding="utf-8",
        )
        return config

    return write


@pytest.fixture
def client_factory():
    class StubClient:
        def __init__(self, *responses):
            self.responses = list(responses)
            self.calls = 0

        def fetch(self, settings):
            index = min(self.calls, len(self.responses) - 1)
            self.calls += 1
            response = self.responses[index]
            if isinstance(response, Exception):
                raise response
            return validate_feed(copy.deepcopy(response))

    return StubClient


@pytest.fixture
def clock():
    class Clock:
        now = datetime(2026, 11, 11, 12, tzinfo=UTC)

        def __call__(self):
            return self.now

        def advance(self, **delta):
            self.now += timedelta(**delta)

    return Clock()
