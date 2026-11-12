from pathlib import Path

from bugalert.config import load_settings


def test_container_example_keeps_writable_state_outside_readonly_config(monkeypatch, tmp_path):
    example = Path(__file__).resolve().parents[1] / "docker" / "config.toml"
    monkeypatch.chdir(tmp_path)
    settings = load_settings(example)

    assert settings.seed_file == Path("/opt/bugalert/bugalerts.json").resolve()
    assert settings.database_file == Path("/var/lib/bugalert/state.sqlite3").resolve()
    assert not settings.database_file.is_relative_to(settings.config_path.parent)
