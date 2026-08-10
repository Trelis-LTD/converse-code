"""Append-only JSONL trace of tool traffic.

Diagnosing a session must not require access to the broker's server-side logs:
this file records every tool call, result, cancellation, and host context push
as it crosses the client, so "the brain never called the tool" and "the call
failed inside the client" become a grep instead of forensic inference.
"""

import json
import logging
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

PATH = Path(tempfile.gettempdir()) / "converse-code-tools.jsonl"


def trace(event: str, **fields) -> None:
    """Best-effort append; tracing must never break the session."""
    try:
        record = {"ts": round(time.time(), 3), "event": event, **fields}
        with PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        log.debug("trace write failed", exc_info=True)
