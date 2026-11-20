"""The release workflow and the bare metal package: pinned build tools, a token only where it is needed."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
BUILD_REQUIREMENTS = ROOT / "packaging" / "build-requirements.txt"


# ---------------------------
#   _steps
# ---------------------------
def _steps(workflow: str) -> dict[str, str]:
    """The text of each step of a workflow, by its name."""
    parts = re.split(r"^      - name: ", workflow, flags=re.MULTILINE)
    return {part.split("\n", 1)[0].strip(): part for part in parts[1:]}


# ---------------------------
#   _requirements
# ---------------------------
def _requirements(path: Path) -> dict[str, list[str]]:
    """Each requirement of a hashed requirements file with its hashes."""
    text = re.sub(r"\\\n\s*", " ", path.read_text(encoding="utf-8"))
    result = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            spec, *options = line.split()
            result[spec] = [option for option in options if option.startswith("--hash=sha256:")]
    return result


def test_build_tools_are_pinned_with_hashes() -> None:
    """Every build tool has an exact version and at least one sha256: a new release of one cannot change the wheel."""
    requirements = _requirements(BUILD_REQUIREMENTS)
    assert {spec.split("==")[0].lower().replace("_", "-") for spec in requirements} >= {
        "build",
        "packaging",
        "pyproject-hooks",
        "setuptools",
    }
    for spec, hashes in requirements.items():
        assert re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][0-9A-Za-z.]*", spec), spec
        assert hashes and all(re.fullmatch(r"--hash=sha256:[0-9a-f]{64}", h) for h in hashes), spec


def test_release_builds_without_fetching_and_keeps_the_token_to_the_upload() -> None:
    """The wheel is built from the hashed tools only, and the write token reaches the upload step alone."""
    workflow = RELEASE.read_text(encoding="utf-8")
    steps = _steps(workflow)
    build = steps["Build the wheel"]
    assert "--require-hashes" in build and "packaging/build-requirements.txt" in build
    assert "--no-isolation" in build and "--upgrade build" not in build
    assert "persist-credentials: false" in steps["Check out the release tag"]
    holders = [name for name, text in steps.items() if "GH_TOKEN" in text]
    assert holders == ["Attach to the release"]
    jobs_header = workflow.split("    steps:", 1)[0]
    assert "GH_TOKEN" not in jobs_header
