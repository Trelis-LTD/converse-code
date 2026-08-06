"""Claude Code hook wiring.

We launch `claude --settings <generated file>` so a Stop hook fires whenever
Claude finishes a turn. The hook simply POSTs its stdin payload (which includes
transcript_path, session_id and last_assistant_message) to our local server —
no polling of Claude's internals, just the documented hook contract.

The URL carries the run's secret token: the hook payload is spoken back to the
dev, so an unauthenticated endpoint would let any local process (or web page)
put words in Claude's mouth.
"""

import json
from pathlib import Path


def write_settings(dir_path: str | Path, hook_url: str) -> Path:
    hook_cmd = (
        "curl -s -X POST --max-time 5 -H 'Content-Type: application/json' "
        f"--data-binary @- '{hook_url}'"
    )
    settings = {
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": hook_cmd}]}],
        }
    }
    path = Path(dir_path) / "converse-code-settings.json"
    path.write_text(json.dumps(settings, indent=2))
    path.chmod(0o600)
    return path
