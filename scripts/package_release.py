#!/usr/bin/env python3
"""Build the ASF Nav Bridge source and installable release archives."""
from __future__ import annotations

import hashlib
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
SOURCE_FILES = (
    ".gitignore", "scripts/package_release.py", ".github/workflows/release.yml",
    "VERSION", "README.md", "CHANGELOG.md", "asf-bridge.py", "asf-bridge.service",
    "config.example.json", "deploy.sh", "deploy.ps1", "inject_nav.py",
    "inject_nginx.py", "nav-asf-snippet.html", "tests/test_bridge.py", "tests/test_deploy.py",
)


def current_version() -> str:
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    code = (ROOT / "asf-bridge.py").read_text(encoding="utf-8")
    if not re.search(rf'(?m)^VERSION = "{re.escape(version)}"$', code):
        raise SystemExit("Bridge code VERSION does not match VERSION file")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit("VERSION must contain a plain semantic version")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    latest = re.search(r"(?m)^##\s+v?(\d+\.\d+\.\d+)", changelog)
    if not latest or latest.group(1) != version:
        raise SystemExit("CHANGELOG first release heading must match the current version")
    if not re.search(rf"(?im)^>\s*版本：\*\*v{re.escape(version)}\*\*(?:\s*[｜|].*)?$", readme):
        raise SystemExit(f"README current version does not match VERSION {version}")
    if not re.search(rf"(?im)^##\s+v?{re.escape(version)}(?:\s+[—-].*)?\s*$", changelog):
        raise SystemExit(f"CHANGELOG latest version does not match VERSION {version}")
    return version


def add_file(archive: zipfile.ZipFile, source: Path, name: str) -> None:
    info = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (0o100755 if name.endswith(".sh") else 0o100644) << 16
    archive.writestr(info, source.read_bytes())


def zip_files(path: Path, files: tuple[str, ...]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in files:
            source = ROOT / name
            if not source.is_file():
                raise SystemExit(f"Required release file is missing: {name}")
            add_file(archive, source, name)


def latest_notes(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(rf"(?m)^##\s+v?{re.escape(version)}(?:\s+[—-].*)?\s*$", text)
    assert match is not None
    remainder = text[match.end():]
    next_heading = re.search(r"(?m)^##\s+", remainder)
    section = remainder[:next_heading.start()] if next_heading else remainder
    return f"# ASF Nav Bridge v{version}\n\n{section.strip()}\n"


def main() -> None:
    if sys.argv[1:] == ["--version"]:
        print(current_version())
        return
    if sys.argv[1:]:
        raise SystemExit("usage: package_release.py [--version]")
    version = current_version()
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    source_zip = dist / f"asf-nav-bridge-v{version}-source.zip"
    install_zip = dist / f"asf-nav-bridge-v{version}-install.zip"
    zip_files(source_zip, SOURCE_FILES)
    zip_files(install_zip, SOURCE_FILES)
    (dist / "release-notes.md").write_text(latest_notes(version), encoding="utf-8")
    with (dist / "SHA256SUMS").open("w", encoding="ascii", newline="\n") as sums:
        for path in (source_zip, install_zip):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            sums.write(f"{digest}  {path.name}\n")


if __name__ == "__main__":
    main()
