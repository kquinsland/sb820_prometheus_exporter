"""Prevent release metadata and the version shipped in the container drifting."""

import json
import tomllib
from pathlib import Path

from util.version import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_release_versions_match():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = [pkg for pkg in lock["package"] if pkg["name"] == project["name"]]
    assert len(packages) == 1
    manifest = json.loads((ROOT / ".release-please-manifest.json").read_text())
    assert project["version"] == packages[0]["version"] == manifest["."] == __version__
