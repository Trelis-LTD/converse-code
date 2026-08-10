import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
VENDOR = ROOT / "converse_code" / "web" / "vendor" / "converse"


def test_client_and_package_metadata_are_apache_licensed():
    license_text = (ROOT / "LICENSE").read_text()
    project = (ROOT / "pyproject.toml").read_text()

    assert "Apache License" in license_text
    assert "Version 2.0" in license_text
    assert 'license = "Apache-2.0"' in project
    assert "MIT License" not in license_text


def test_vendored_sdk_carries_license_notice_and_complete_attributions():
    package = json.loads((VENDOR / "package.json").read_text())
    provenance = json.loads((VENDOR / "UPSTREAM.json").read_text())
    third_party = VENDOR / "THIRD_PARTY_LICENSES"

    assert package["license"] == "Apache-2.0"
    assert package["version"] == "0.10.0"
    assert provenance["commit"] == "2651c7cba65794bcf8675118d1496b593cfa89ba"
    notice = " ".join((VENDOR / "NOTICE").read_text().split())
    assert "hosted Converse service" in notice
    assert len(list(third_party.glob("*"))) >= 13


def test_readme_states_the_open_client_service_boundary():
    readme = (ROOT / "README.md").read_text()

    assert "hosted Converse service" in readme
    assert "server-side software" in readme
