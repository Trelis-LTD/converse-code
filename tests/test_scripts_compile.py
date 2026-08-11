"""Release and diagnostic scripts must at least parse in the supported Python runtime."""

from pathlib import Path


def test_scripts_compile():
    scripts = Path(__file__).parents[1] / "scripts"
    for path in sorted(scripts.glob("*.py")):
        compile(path.read_text(), str(path), "exec")
