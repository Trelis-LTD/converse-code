"""Shared waiting and environment probes for the deterministic suite."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from typing import Callable


async def wait_until(predicate: Callable[[], object], *, timeout: float = 2.0,
                     interval: float = 0.01, describe: Callable[[], str] | None = None) -> None:
    """Wait for an observable condition, never for a scheduler tick count: how many event-loop
    passes an operation needs varies across supported Python versions. On expiry, fail with the
    condition's own description instead of an anonymous TimeoutError, and without having pegged
    a core busy-spinning."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            detail = f": {describe()}" if describe else ""
            raise AssertionError(f"condition not reached within {timeout}s{detail}")
        await asyncio.sleep(interval)


def node_typescript_support() -> tuple[bool, str]:
    """Whether the local node can run TypeScript entry files without flags: default type
    stripping shipped in 23.6 and was backported to 22.18. Returns (capable, detail)."""
    node = shutil.which("node")
    if node is None:
        return False, "node is not installed"
    try:
        raw = subprocess.run([node, "--version"], capture_output=True, text=True,
                             timeout=10, check=False).stdout.strip()
    except OSError as exc:
        return False, f"node --version failed: {exc}"
    match = re.match(r"v(\d+)\.(\d+)", raw)
    if match is None:
        return False, f"unrecognized node version {raw!r}"
    major, minor = int(match.group(1)), int(match.group(2))
    capable = (major, minor) >= (23, 6) or ((major, minor) >= (22, 18) and major == 22)
    return capable, f"found node {raw}"
