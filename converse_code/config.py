"""API key storage: CONVERSE_API_KEY env var, else ~/.config/converse-code/config.json."""

import json
import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("CONVERSE_CODE_CONFIG_DIR", "~/.config/converse-code")).expanduser()
CONFIG_PATH = CONFIG_DIR / "config.json"


def get_api_key() -> str | None:
    key = os.environ.get("CONVERSE_API_KEY")
    if key:
        return key
    try:
        return json.loads(CONFIG_PATH.read_text()).get("api_key")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_api_key(key: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = {}
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    data["api_key"] = key
    CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n")
    CONFIG_PATH.chmod(0o600)
