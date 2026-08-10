"""Append-only JSONL trace of tool traffic.

Diagnosing a session must not require access to the broker's server-side logs:
this file records every tool call, result, cancellation, and host context push
as it crosses the client, so "the brain never called the tool" and "the call
failed inside the client" become a grep instead of forensic inference.
"""

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Per-session file in a user-only directory: tool traffic includes code
# contents and dictated text, so it must not land world-readable in shared
# /tmp, and concurrent sessions must not interleave.
TRACE_DIR = Path.home() / ".config" / "converse-code" / "traces"
PATH = TRACE_DIR / f"session-{os.getpid()}.jsonl"


def trace(event: str, **fields) -> None:
    """Best-effort append; tracing must never break the session."""
    try:
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        TRACE_DIR.chmod(0o700)
        record = {"ts": round(time.time(), 3), "event": event, **fields}
        fd = os.open(PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(record, default=str) + "\n").encode())
        finally:
            os.close(fd)
    except Exception:
        log.debug("trace write failed", exc_info=True)
