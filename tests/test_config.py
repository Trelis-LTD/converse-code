import json

from converse_code import config


def test_non_object_or_non_string_config_never_becomes_an_api_key(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.delenv("CONVERSE_API_KEY", raising=False)

    for value in ([], {"api_key": 42}, {"api_key": ""}):
        path.write_text(json.dumps(value))
        assert config.get_api_key() is None


def test_saving_over_non_object_config_produces_a_valid_private_config(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    path.write_text("[]")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", path)

    config.save_api_key("ck_valid")

    assert json.loads(path.read_text()) == {"api_key": "ck_valid"}
    assert path.stat().st_mode & 0o777 == 0o600
