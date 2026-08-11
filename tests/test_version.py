"""Release metadata must agree with the importable package version."""

import tomllib
from pathlib import Path

from converse_code import __version__


def test_package_version_matches_project_metadata():
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert __version__ == pyproject["project"]["version"]
