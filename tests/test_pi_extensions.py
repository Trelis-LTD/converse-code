import subprocess

import pytest

from support import node_typescript_support


def test_visible_pi_extension_uses_semantic_controls_and_remote_approvals():
    capable, detail = node_typescript_support()
    if not capable:
        # An environment whose node cannot load TypeScript entry files proves nothing about the
        # extension contract -- skip loudly with the requirement named. The check still runs
        # red-on-failure in the mandated verification ladder (scripts/browser_e2e.py), which
        # refuses to run at all without a capable node.
        pytest.skip(f"node >= 22.18 (or 23.6+) with default TypeScript type stripping is "
                    f"required for the Pi extension contract check; {detail}")
    result = subprocess.run(
        ["node", "tests/pi_extensions_check.mjs"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
