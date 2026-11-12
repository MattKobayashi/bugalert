import pytest

from bugalert.config import ConfigError, load_settings


def test_loads_relative_paths_and_external_versions(write_config):
    path = write_config()
    settings = load_settings(path)
    assert settings.versions == ("4.31.1M", "4.32.1F")
    assert settings.seed_file == path.parent / "seed.json"
    assert settings.database_file == path.parent / "state.sqlite3"
    assert settings.refresh_seconds == 30


@pytest.mark.parametrize(
    "versions", [[], [123], ["not-eos"], ["4.31.1; rm"], ["4.31.1M", "4.31.1"]]
)
def test_rejects_invalid_or_duplicate_versions(write_config, versions):
    with pytest.raises(ConfigError):
        load_settings(write_config(versions))


@pytest.mark.parametrize("value", [0, -1, 29, True, "900", 86401])
def test_rejects_invalid_refresh_intervals(write_config, value):
    with pytest.raises(ConfigError, match="refresh_seconds"):
        load_settings(write_config(refresh_seconds=value))


@pytest.mark.parametrize("env", ["secret value", "not-a-variable", "", 123])
def test_rejects_invalid_token_variable_names(write_config, env):
    with pytest.raises(ConfigError, match="environment variable"):
        load_settings(write_config(token_env=env))


def test_accepts_special_eos_branches(write_config):
    settings = load_settings(write_config(["4.31.1FX-Test.2", "4.21.2.5F", "4.10.0-7150"]))
    assert len(settings.versions) == 3


def test_rejects_unknown_keys(write_config):
    with pytest.raises(ConfigError, match="Unknown setting"):
        load_settings(write_config(refresh_second=900))


def test_reports_malformed_toml(write_config):
    path = write_config()
    path.write_text('[eos]\nversions = ["4.31.1M"', encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid TOML"):
        load_settings(path)


def test_reports_missing_config(tmp_path):
    with pytest.raises(ConfigError, match="Cannot read configuration"):
        load_settings(tmp_path / "missing.toml")


def test_invalid_paths_are_configuration_errors(write_config):
    with pytest.raises(ConfigError):
        load_settings(write_config(seed_file="bad\x00path"))
    with pytest.raises(ConfigError):
        load_settings("bad\x00config")
