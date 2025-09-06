#!/usr/bin/env python
# https://gist.github.com/yhoiseth/c80c1e44a7036307e424fce616eed25e?permalink_comment_id=5335497
##
# As of right now (very late 2024) there is no simple way to tell UV to update all dependencies in a project.
# You're basically stuck with rm/add for now.
# This script automates that process.
# It _should_ be obviated once this issue is resolved:
#   https://github.com/astral-sh/uv/issues/6794
##

import re
import subprocess
from pathlib import Path

import tomllib


def uv(subcommand: str, packages: list[str], group: str | None):
    extra_arguments = []
    if group:
        extra_arguments.extend(["--group", group])

    subprocess.check_call(["uv", subcommand, *packages, "--no-sync"] + extra_arguments)


def main():
    """WARNING:
    from the `pyproject.toml` file, this may delete:
        - comments
        - upper bounds etc
        - markers
        - ordering of dependencies
    """
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    package_name_pattern = re.compile(r"^([-a-zA-Z\d]+)(\[[-a-zA-Z\d,]+])?")
    for group, dependencies in {
        None: pyproject["project"]["dependencies"],
        **pyproject["dependency-groups"],
    }.items():
        to_remove = []
        to_add = []
        for dependency in dependencies:
            package_match = package_name_pattern.match(dependency)
            assert package_match, f"invalid package name '{dependency}'"
            package, extras = package_match.groups()
            to_remove.append(package)
            to_add.append(f"{package}{extras or ''}")
        uv("remove", to_remove, group=group)
        uv("add", to_add, group=group)
    subprocess.check_call(["uv", "sync"])


if __name__ == "__main__":
    main()
