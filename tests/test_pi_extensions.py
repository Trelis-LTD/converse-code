import subprocess


def test_visible_pi_extension_uses_semantic_controls_and_remote_approvals():
    result = subprocess.run(
        ["node", "tests/pi_extensions_check.mjs"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Pi extension contract: passed" in result.stdout
