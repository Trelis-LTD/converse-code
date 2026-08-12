"""Opt-in, locally redacted JSONL evidence for one Converse Code run."""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|authorization|credential|password|secret|token)$", re.I)
CONVERSE_KEY = re.compile(r"\bck_[A-Za-z0-9._-]+")
BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
QUERY_TOKEN = re.compile(r"([?&]t=)[^&#\s]+")
INLINE_SECRET = re.compile(
    r"(?i)(\b[A-Z0-9_-]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)"
    r"[A-Z0-9_-]*\s*[=:]\s*)(['\"]?)([^\s,;'\"]+)(['\"]?)"
)
SECRET_FLAG = re.compile(
    r"(?i)(--(?:api[-_]?key|token|password|secret|credential)(?:=|\s+))([^\s]+)"
)
KNOWN_SECRET = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|AKIA[A-Z0-9]{16})\b"
)


def _redact_string(value: str) -> str:
    value = CONVERSE_KEY.sub("[REDACTED]", value)
    value = BEARER.sub("Bearer [REDACTED]", value)
    value = QUERY_TOKEN.sub(r"\1[REDACTED]", value)
    value = INLINE_SECRET.sub(lambda match: f"{match.group(1)}[REDACTED]", value)
    value = SECRET_FLAG.sub(lambda match: f"{match.group(1)}[REDACTED]", value)
    return KNOWN_SECRET.sub("[REDACTED]", value)


def _redact(value: Any, *, key: str = "") -> Any:
    if SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _redact(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return repr(value)


class SessionTrace:
    """Append trace records immediately so a crashed or interrupted run remains debuggable."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_id = secrets.token_hex(8)
        descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        self._file = os.fdopen(descriptor, "a", encoding="utf-8", buffering=1)

    def record(self, source: str, event: str, **data: Any) -> None:
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z",
        )
        entry = {
            "timestamp": timestamp,
            "session_id": self.session_id,
            "source": source,
            "event": event,
            "data": _redact(data),
        }
        self._file.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._file.flush()

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()
