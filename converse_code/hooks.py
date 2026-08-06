"""Claude Code hook wiring.

We launch `claude --settings <generated file>` so a Stop hook fires whenever
Claude finishes a turn. The hook simply POSTs its stdin payload (which
includes transcript_path and session_id) to our local server — no polling of
Claude's internals, just the documented hook contract.
"""

import json
from pathlib import Path


def write_settings(dir_path: str | Path, port: int) -> Path:
    hook_cmd = (
        "curl -s -X POST --max-time 5 -H 'Content-Type: application/json' "
        f"--data-binary @- http://127.0.0.1:{port}/hook/stop"
    )
    settings = {
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": hook_cmd}]}],
        }
    }
    path = Path(dir_path) / "converse-code-settings.json"
    path.write_text(json.dumps(settings, indent=2))
    return path
