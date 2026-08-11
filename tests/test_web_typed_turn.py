import shutil
import subprocess
from pathlib import Path

import pytest


CHECK = Path(__file__).parent / "web_typed_turn_check.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_typed_turn_acknowledgement_serialization_and_failure():
    proc = subprocess.run(["node", str(CHECK)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "typed turn lifecycle: OK" in proc.stdout
