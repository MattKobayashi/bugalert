"""The supplied Arista POST protocol, with bounded, validated input."""

import ast
import base64
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    from bugalert.config import Settings

FEED_URL = "https://www.arista.com/custom_data/bug-alert/alertBaseDownloadApi.php"
MAX_FEED_BYTES = 64 * 1024 * 1024


class FeedError(ValueError):
    """A safe error message, without response bodies or credentials."""


class MissingTokenError(FeedError):
    pass


@dataclass(frozen=True, slots=True)
class FeedData:
    payload: dict[str, Any]

    @property
    def bugs(self) -> list[dict]:
        return self.payload["bugs"]

    @property
    def generation_id(self) -> str:
        return self.payload["genId"]

    @property
    def release_date(self) -> str:
        return self.payload["releaseDate"]


def _escape_literal_newlines(text: str) -> str:
    """Repair the supplied pretty-printed repr, not arbitrary executable Python."""
    result, quote, escaped = [], None, False
    for char in text:
        if quote:
            if escaped:
                result.append(char)
                escaped = False
            elif char == "\\":
                result.append(char)
                escaped = True
            elif char == quote:
                result.append(char)
                quote = None
            else:
                result.append({"\n": "\\n", "\r": "\\r"}.get(char, char))
        else:
            result.append(char)
            if char in ("'", '"'):
                quote = char
    return "".join(result)


def parse_feed(text: str | bytes, *, allow_legacy: bool = False) -> FeedData:
    if len(text) > MAX_FEED_BYTES:
        raise FeedError("The feed exceeds the 64 MiB size limit.")
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise FeedError("The feed is not valid UTF-8.") from error
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as error:
        if not allow_legacy:
            raise FeedError("Arista did not return a valid JSON feed.") from error
        try:
            payload = ast.literal_eval(_escape_literal_newlines(text))
        except (SyntaxError, ValueError, RecursionError) as literal_error:
            raise FeedError(
                "The local seed is neither valid JSON nor a supported Python dump."
            ) from literal_error
    return validate_feed(payload)


def validate_feed(payload: Any) -> FeedData:
    if not isinstance(payload, dict):
        raise FeedError("The feed must be an object.")
    if not isinstance(payload.get("genId"), str) or not payload["genId"]:
        raise FeedError("The feed is missing its generation ID.")
    try:
        date.fromisoformat(payload.get("releaseDate", ""))
    except (ValueError, TypeError) as error:
        raise FeedError("The feed has an invalid release date.") from error
    if not isinstance(payload.get("bugs"), list):
        raise FeedError("The feed is missing its bugs list.")
    if not isinstance(payload.get("releaseGraph"), list):
        raise FeedError("The feed is missing the release graph needed for safe version matching.")
    for edge in payload["releaseGraph"]:
        if (
            not isinstance(edge, list)
            or len(edge) != 2
            or not all(isinstance(node, str) and node for node in edge)
        ):
            raise FeedError("The feed contains an invalid release graph edge.")
    releases = payload.get("releases", [])
    if not isinstance(releases, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("release"), str)
        or not isinstance(item.get("product"), str)
        for item in releases
    ):
        raise FeedError("The feed contains an invalid releases list.")

    seen = set()
    for bug in payload["bugs"]:
        if not isinstance(bug, dict) or type(bug.get("bugId")) is not int or bug["bugId"] <= 0:
            raise FeedError("The feed contains an invalid bug ID.")
        if bug["bugId"] in seen:
            raise FeedError("The feed contains duplicate bug IDs.")
        seen.add(bug["bugId"])
        for field in ("product", "severity", "alertSummary"):
            if not isinstance(bug.get(field), str):
                raise FeedError(f"The feed contains an invalid {field} field.")
        for field in ("versionIntroduced", "versionFixed"):
            value = bug.get(field)
            if not isinstance(value, list) or any(
                not isinstance(version, str) or not version for version in value
            ):
                raise FeedError(f"The feed contains an invalid {field} list.")
        if not bug["versionIntroduced"]:
            raise FeedError("The feed contains a bug without an introduced release.")
        for field in ("cve", "securityAdvisoryURL", "alertNote", "releaseNote"):
            if field in bug and not isinstance(bug[field], str):
                raise FeedError(f"The feed contains an invalid {field} field.")
    return FeedData(payload)


def load_seed(path: Path) -> FeedData:
    try:
        if path.stat().st_size > MAX_FEED_BYTES:
            raise FeedError("The local seed exceeds the 64 MiB size limit.")
        return parse_feed(path.read_bytes(), allow_legacy=True)
    except OSError as error:
        raise FeedError(f"Cannot read the local seed file: {path.name}.") from error


def download_feed(token: str, timeout_seconds: int = 30) -> FeedData:
    if not token.strip():
        raise MissingTokenError("An Arista feed token is required.")
    credentials = base64.b64encode(token.strip().encode()).decode("ascii")
    try:
        response = requests.post(
            FEED_URL,
            data=json.dumps({"token_auth": credentials, "file_version": "2"}),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=(min(5, timeout_seconds), timeout_seconds),
        )
        response.raise_for_status()
    except requests.Timeout as error:
        raise FeedError(
            "The Arista feed request timed out. The previous feed is retained."
        ) from error
    except requests.HTTPError as error:
        raise FeedError(
            f"Arista returned HTTP {error.response.status_code}. Check your feed access and token."
        ) from error
    except requests.RequestException as error:
        raise FeedError("Cannot reach the Arista feed. The previous feed is retained.") from error
    return parse_feed(response.content)


class AristaClient:
    def fetch(self, settings: Settings) -> FeedData:
        token = os.environ.get(settings.token_env, "")
        if not token.strip():
            raise MissingTokenError(
                f"Set {settings.token_env} in the server environment to enable live feed checks."
            )
        return download_feed(token, settings.timeout_seconds)
