"""The release workflow and the bare metal package: pinned build tools, a token only where it is needed."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
BUILD_REQUIREMENTS = ROOT / "packaging" / "build-requirements.txt"
RUNTIME_REQUIREMENTS = ROOT / "packaging" / "requirements.txt"
INSTALL = ROOT / "packaging" / "install.sh"


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


def test_runtime_dependencies_are_pinned_with_hashes_within_pyproject() -> None:
    """Every runtime dependency of the bare metal package has an exact version and a hash; aiohttp stays in pyproject's range."""
    from packaging.requirements import Requirement
    from packaging.version import Version

    requirements = _requirements(RUNTIME_REQUIREMENTS)
    names = {spec.split("==")[0].lower().replace("_", "-") for spec in requirements}
    assert names >= {
        "aiohttp",
        "aiohappyeyeballs",
        "aiosignal",
        "attrs",
        "frozenlist",
        "multidict",
        "propcache",
        "yarl",
        "idna",
    }
    for spec, hashes in requirements.items():
        assert re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][0-9A-Za-z.]*", spec), spec
        assert len(hashes) >= 1 and all(re.fullmatch(r"--hash=sha256:[0-9a-f]{64}", h) for h in hashes), spec
    declared = re.search(r'dependencies = \["(aiohttp[^"]*)"\]', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert declared is not None
    pinned = next(spec for spec in requirements if spec.startswith("aiohttp=="))
    assert Version(pinned.split("==")[1]) in Requirement(declared.group(1)).specifier


def test_the_tarball_carries_the_dependencies_and_install_uses_only_them() -> None:
    """The release puts the hashed wheels in the tarball; install.sh installs from them alone, never from PyPI."""
    steps = _steps(RELEASE.read_text(encoding="utf-8"))
    download = steps["Download the Python dependencies"]
    assert "pip download" in download and "--require-hashes" in download and "packaging/requirements.txt" in download
    assert "--platform any" in download and "--only-binary :all:" in download
    pack = steps["Pack the tarball"]
    assert "wheels" in pack and "packaging/requirements.txt" in pack
    script = re.sub(r"\\\n\s*", " ", INSTALL.read_text(encoding="utf-8"))  # continued lines as one
    installs = [line for line in script.splitlines() if '/pip" install' in line]
    assert installs and all("--no-index" in line for line in installs)
    assert any("--require-hashes" in line and "requirements.txt" in line for line in installs)


def test_the_user_is_removed_only_with_its_files() -> None:
    """--uninstall keeps the user with the data it owns; --purge deletes the data first, then the user.

    A user removed while $DATA/web stays would leave its files to an
    orphan UID, which a system account made later could receive.
    """
    script = INSTALL.read_text(encoding="utf-8")
    uninstall = script.split("uninstall() {", 1)[1].split("\n}\n", 1)[0]
    assert "userdel" not in uninstall
    purge = script.split("    --purge)", 1)[1].split(";;", 1)[0]
    assert "userdel" in purge and purge.index('rm -rf "$ETC" "$DATA"') < purge.index("userdel")
    keep = script.split("    --uninstall)", 1)[1].split(";;", 1)[0]
    assert "userdel" not in keep


def test_upstream_watches_every_pinned_program() -> None:
    """upstream.yml checks hddfancontrol, hd-idle and sdparm, the three programs the Dockerfile pins by hand."""
    workflow = (ROOT / ".github" / "workflows" / "upstream.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    pinned = [arg for arg in re.findall(r"^ARG ([A-Z]+_VERSION)=", dockerfile, flags=re.MULTILINE) if arg != "APP_VERSION"]
    assert len(pinned) == 3
    for arg in pinned:
        assert arg in workflow, arg
    assert "SDPARM_SHA256" in workflow and "sources.debian.org/api/src/sdparm" in workflow
