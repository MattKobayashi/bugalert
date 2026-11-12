"""Validated, external configuration. Credentials never belong in this file."""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bugalert.versions import canonical_version


class ConfigError(ValueError):
    """An actionable configuration error, safe to display in the dashboard."""


@dataclass(frozen=True, slots=True)
class Settings:
    config_path: Path
    versions: tuple[str, ...]
    refresh_seconds: int
    timeout_seconds: int
    new_window_hours: int
    token_env: str
    seed_file: Path
    database_file: Path


def _table(data: dict[str, Any], name: str, allowed: set[str]) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a TOML table.")
    unexpected = value.keys() - allowed
    if unexpected:
        raise ConfigError(f"Unknown setting in [{name}]: {', '.join(sorted(unexpected))}.")
    return value


def _integer(table: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    value = table.get(key, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigError(f"feed.{key} must be an integer between {minimum} and {maximum}.")
    return value


def _path(table: dict, key: str, default: str, base: Path) -> Path:
    value = table.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"feed.{key} must be a non-empty file path.")
    try:
        return (base / Path(value).expanduser()).resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise ConfigError(f"feed.{key} must be a valid file path.") from error


def load_settings(path: str | Path) -> Settings:
    try:
        path = Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise ConfigError("The configuration file path is invalid.") from error
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except OSError as error:
        raise ConfigError(f"Cannot read configuration: {path}.") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"Invalid TOML in {path.name}: {error}") from error

    if data.keys() - {"eos", "feed"}:
        raise ConfigError("Only [eos] and [feed] tables are supported.")
    eos = _table(data, "eos", {"versions"})
    feed = _table(
        data,
        "feed",
        {
            "refresh_seconds",
            "timeout_seconds",
            "new_window_hours",
            "token_env",
            "seed_file",
            "database_file",
        },
    )
    versions = eos.get("versions")
    if not isinstance(versions, list) or not 1 <= len(versions) <= 50:
        raise ConfigError("eos.versions must contain between 1 and 50 EOS release strings.")
    pattern = r"4\.\d+\.\d+(?:\.\d+)*(?:[A-Za-z][A-Za-z0-9._-]*|-[A-Za-z0-9._-]+)?"
    for version in versions:
        if not isinstance(version, str) or not re.fullmatch(pattern, version):
            raise ConfigError("Each EOS release must look like 4.33.5M or 4.33.1FX-R4.4.")
    if len({canonical_version(version) for version in versions}) != len(versions):
        raise ConfigError("eos.versions contains duplicate releases (F/M suffixes are equivalent).")

    token_env = feed.get("token_env", "BUGALERT_ARISTA_TOKEN")
    if not isinstance(token_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
        raise ConfigError("feed.token_env must be an environment variable name, not a token.")

    return Settings(
        config_path=path,
        versions=tuple(versions),
        refresh_seconds=_integer(feed, "refresh_seconds", 900, 30, 86400),
        timeout_seconds=_integer(feed, "timeout_seconds", 30, 1, 120),
        new_window_hours=_integer(feed, "new_window_hours", 72, 1, 8760),
        token_env=token_env,
        seed_file=_path(feed, "seed_file", "bugalerts.json", path.parent),
        database_file=_path(feed, "database_file", ".bugalert/state.sqlite3", path.parent),
    )
