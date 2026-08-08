"""Copy a licensed @trelis/converse source release into the static web client.

Usage:
    uv run scripts/vendor_converse_sdk.py /path/to/sdk/browser --commit <git-sha>
    uv run scripts/vendor_converse_sdk.py /path/to/sdk/browser --commit <git-sha> --check

The source directory must be the preferred-form SDK tree, not a minified bundle,
and must include its Apache license, NOTICE, and third-party license directory.
"""

import argparse
import filecmp
import json
import re
import shutil
from pathlib import Path


ROOT = Path(__file__).parents[1]
DEST = ROOT / "converse_code" / "web" / "vendor" / "converse"
METADATA = ("LICENSE", "NOTICE", "CHANGELOG.md", "package.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Extracted sdk/browser source directory")
    parser.add_argument("--commit", required=True, help="Full upstream Git commit SHA")
    parser.add_argument("--check", action="store_true", help="Fail if the committed copy differs")
    return parser.parse_args()


def validate_source(source: Path) -> dict:
    missing = [name for name in METADATA if not (source / name).is_file()]
    if missing:
        raise SystemExit(f"SDK source is missing required metadata: {', '.join(missing)}")
    package = json.loads((source / "package.json").read_text())
    if package.get("license") != "Apache-2.0":
        raise SystemExit("Refusing to vendor SDK source that is not Apache-2.0")
    if not (source / "THIRD_PARTY_LICENSES").is_dir():
        raise SystemExit("SDK source has no THIRD_PARTY_LICENSES directory")
    if not list((source / "src").glob("*.js")):
        raise SystemExit("SDK source has no preferred-form JavaScript in src/")
    return package


def provenance(package: dict, commit: str) -> str:
    data = {
        "package": package["name"],
        "version": package["version"],
        "repository": "https://github.com/Trelis-LTD/voice-loop-pro",
        "path": "sdk/browser",
        "commit": commit,
    }
    return json.dumps(data, indent=2) + "\n"


def wanted_files(source: Path) -> dict[Path, Path]:
    files = {source / name: DEST / name for name in METADATA}
    files.update({path: DEST / path.name for path in sorted((source / "src").glob("*.js"))})
    for path in sorted((source / "THIRD_PARTY_LICENSES").iterdir()):
        if path.is_file():
            files[path] = DEST / "THIRD_PARTY_LICENSES" / path.name
    return files


def check(source: Path, package: dict, commit: str) -> None:
    drift = []
    for src, dest in wanted_files(source).items():
        if not dest.is_file() or not filecmp.cmp(src, dest, shallow=False):
            drift.append(str(dest.relative_to(ROOT)))
    upstream_path = DEST / "UPSTREAM.json"
    if not upstream_path.is_file() or upstream_path.read_text() != provenance(package, commit):
        drift.append(str(upstream_path.relative_to(ROOT)))
    if drift:
        raise SystemExit("Vendored SDK is stale:\n  " + "\n  ".join(drift))
    print(f"Vendored @trelis/converse {package['version']} is in sync with {commit[:12]}.")


def vendor(source: Path, package: dict, commit: str) -> None:
    DEST.mkdir(parents=True, exist_ok=True)
    third_party = DEST / "THIRD_PARTY_LICENSES"
    if third_party.exists():
        shutil.rmtree(third_party)
    third_party.mkdir()

    keep = {dest.name for dest in wanted_files(source).values() if dest.parent == DEST}
    keep.update({"README.md", "UPSTREAM.json", "THIRD_PARTY_LICENSES"})
    for old in DEST.iterdir():
        if old.name not in keep and old.is_file():
            old.unlink()
    for src, dest in wanted_files(source).items():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    (DEST / "UPSTREAM.json").write_text(provenance(package, commit))
    print(f"Vendored @trelis/converse {package['version']} from {commit[:12]} with notices.")


def main() -> None:
    args = parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        raise SystemExit("--commit must be a full lowercase Git commit SHA")
    source = args.source.resolve()
    package = validate_source(source)
    if args.check:
        check(source, package, args.commit)
    else:
        vendor(source, package, args.commit)


if __name__ == "__main__":
    main()
