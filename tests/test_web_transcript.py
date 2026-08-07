import shutil
import subprocess
from pathlib import Path

import pytest


CHECK = Path(__file__).parent / "web_transcript_check.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_interrupted_assistant_transcript_is_revised_in_place():
    proc = subprocess.run(
        ["node", str(CHECK)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "assistant transcript revisions: OK" in proc.stdout
