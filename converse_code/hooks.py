"""Claude Code hook wiring.

We launch `claude --settings <generated file>` with native HTTP hooks. A
UserPromptSubmit hook acknowledges that injected text was actually submitted,
and a Stop hook reports turn completion. Claude POSTs their structured payloads
directly to our local server — no shell bridge and no polling of internals.

The URL carries the run's secret token: the hook payload is spoken back to the
dev, so an unauthenticated endpoint would let any local process (or web page)
put words in Claude's mouth.
"""

import json
from pathlib import Path


def write_settings(dir_path: str | Path, stop_url: str, prompt_submit_url: str,
                   permission_request_url: str, stop_failure_url: str) -> Path:
    def http_hook(url: str) -> dict:
        return {"type": "http", "url": url, "timeout": 5}

    settings = {
        "hooks": {
            "Stop": [{"hooks": [http_hook(stop_url)]}],
            "UserPromptSubmit": [{"hooks": [http_hook(prompt_submit_url)]}],
            "PermissionRequest": [{"hooks": [http_hook(permission_request_url)]}],
            "StopFailure": [{"hooks": [http_hook(stop_failure_url)]}],
        }
    }
    path = Path(dir_path) / "converse-code-settings.json"
    path.write_text(json.dumps(settings, indent=2))
    path.chmod(0o600)
    return path
