import base64
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from bugalert.config import load_settings
from bugalert.feed import (
    AristaClient,
    FeedError,
    MissingTokenError,
    download_feed,
    load_seed,
    parse_feed,
    validate_feed,
)


def test_parses_strict_json(feed_payload):
    assert parse_feed(json.dumps(feed_payload)).generation_id == "initial-generation"


def test_legacy_dump_is_local_only(feed_payload):
    text = repr(feed_payload)
    with pytest.raises(FeedError, match="valid JSON"):
        parse_feed(text)
    assert parse_feed(text, allow_legacy=True).bugs[0]["bugId"] == 100


def test_repairs_physical_newlines_in_legacy_strings(feed_payload):
    text = repr(feed_payload).replace("The full note.\\nWith", "The full note.\nWith")
    assert parse_feed(text, allow_legacy=True).bugs[0]["alertNote"] == (
        "The full note.\nWith another line."
    )


def test_legacy_loader_never_executes_python(tmp_path):
    marker = tmp_path / "should-not-exist"
    with pytest.raises(FeedError):
        parse_feed(f"__import__('pathlib').Path({str(marker)!r}).touch()", allow_legacy=True)
    assert not marker.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("bugs", {}),
        ("releaseGraph", None),
        ("genId", ""),
        ("releaseDate", "yesterday"),
    ],
)
def test_rejects_invalid_feed_shape(feed_payload, field, value):
    feed_payload[field] = value
    with pytest.raises(FeedError):
        validate_feed(feed_payload)


def test_rejects_duplicate_bugs(feed_payload):
    feed_payload["bugs"].append(feed_payload["bugs"][0])
    with pytest.raises(FeedError, match="duplicate"):
        validate_feed(feed_payload)


def test_rejects_missing_introduction(feed_payload):
    feed_payload["bugs"][0]["versionIntroduced"] = []
    with pytest.raises(FeedError, match="introduced"):
        validate_feed(feed_payload)


def test_download_preserves_original_post_protocol(monkeypatch, feed_payload):
    response = Mock(content=json.dumps(feed_payload).encode())
    post = Mock(return_value=response)
    monkeypatch.setattr("bugalert.feed.requests.post", post)
    assert download_feed("synthetic-token", 20).generation_id == "initial-generation"
    kwargs = post.call_args.kwargs
    assert json.loads(kwargs["data"]) == {
        "token_auth": base64.b64encode(b"synthetic-token").decode(),
        "file_version": "2",
    }
    assert kwargs["timeout"] == (5, 20)
    response.raise_for_status.assert_called_once()


@pytest.mark.parametrize(
    "error", [requests.Timeout("sensitive"), requests.ConnectionError("sensitive")]
)
def test_network_errors_do_not_expose_response_or_token(monkeypatch, error):
    monkeypatch.setattr("bugalert.feed.requests.post", Mock(side_effect=error))
    with pytest.raises(FeedError) as caught:
        download_feed("synthetic-token")
    assert "synthetic-token" not in str(caught.value)
    assert "sensitive" not in str(caught.value)


def test_http_errors_are_actionable_but_sanitized(monkeypatch):
    response = Mock(status_code=403)
    response.raise_for_status.side_effect = requests.HTTPError("sensitive body", response=response)
    monkeypatch.setattr("bugalert.feed.requests.post", Mock(return_value=response))
    with pytest.raises(FeedError, match="HTTP 403") as caught:
        download_feed("synthetic-token")
    assert "sensitive" not in str(caught.value)


def test_missing_token_never_contacts_arista(write_config, monkeypatch):
    post = Mock()
    monkeypatch.setattr("bugalert.feed.requests.post", post)
    with pytest.raises(MissingTokenError, match="BUGALERT_ARISTA_TOKEN"):
        AristaClient().fetch(load_settings(write_config()))
    post.assert_not_called()


def test_supplied_dump_is_supported_without_modifying_it():
    path = Path(__file__).resolve().parents[1] / "bugalerts.json"
    before = path.read_bytes()
    feed = load_seed(path)
    assert len(feed.bugs) > 2000
    assert any(bug["product"] == "eos" for bug in feed.bugs)
    assert path.read_bytes() == before
