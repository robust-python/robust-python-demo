"""Provide deterministic release operations for robust-python-demo."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict
from dataclasses import dataclass
from email.parser import Parser
from enum import Enum
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECT_NAME = "robust-python-demo"
PACKAGE_NAME = "robust_python_demo"
RUST_PROJECT_NAME = "robust-python-demo-core"
PUBLISH_RUST_PACKAGE = "False".lower() == "true"
PYPROJECT_PATH = Path("pyproject.toml")
CHANGELOG_PATH = Path("CHANGELOG.md")
UV_LOCK_PATH = Path("uv.lock")
RUST_MANIFEST_PATH = Path("rust/Cargo.toml")
RUST_LOCK_PATH = Path("rust/Cargo.lock")
ARTIFACT_MANIFEST_NAME = "release-artifacts.json"
_RELEASE_OWNED_PATHS = (
    PYPROJECT_PATH,
    UV_LOCK_PATH,
    CHANGELOG_PATH,
    RUST_MANIFEST_PATH,
    RUST_LOCK_PATH,
)
_VERSION_PATTERN = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:(?P<phase>a|b|rc)(?P<number>0|[1-9]\d*))?$"
)
_CARGO_VERSION_PATTERN = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<phase>alpha|beta|rc)\.(?P<number>0|[1-9]\d*))?$"
)
_CHANGELOG_HEADING_PATTERN = re.compile(
    r"^##[ \t]+v(?P<version>\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?)"
    r"(?=$|[ \t(\-\u2013\u2014])[^\r\n]*(?:\r?\n|$)",
    re.MULTILINE,
)
_LEVEL_TWO_HEADING_PATTERN = re.compile(r"^##[ \t]+[^\r\n]*(?:\r?\n|$)", re.MULTILINE)
_PROJECT_VERSION_PATTERN = re.compile(r"(?ms)^\[project\][ \t]*\r?\n(?P<body>.*?)(?=^\[|\Z)")
_TOML_VERSION_PATTERN = re.compile(r'(?m)^version[ \t]*=[ \t]*"(?P<version>[^"]+)"[ \t]*$')
_WHEEL_FILENAME_PATTERN = re.compile(
    r"^(?P<distribution>.+?)-(?P<version>[^-]+)(?:-[^-]+)?-"
    r"(?P<python_tag>[^-]+)-(?P<abi_tag>[^-]+)-(?P<platform_tag>[^-]+)\.whl$"
)


class ReleaseError(RuntimeError):
    """Base class for release contract failures."""


class InvalidVersionError(ReleaseError):
    """Raised when a version is outside the supported PEP 440 subset."""


class RepositoryStateError(ReleaseError):
    """Raised when the repository is not safe for a release operation."""


class VersionMismatchError(ReleaseError):
    """Raised when release metadata does not describe one version."""


class ChangelogSectionError(ReleaseError):
    """Raised when the requested changelog section is not unique."""


class MissingChangelogSectionError(ChangelogSectionError):
    """Raised when the requested changelog section is absent."""


class DuplicateChangelogSectionError(ChangelogSectionError):
    """Raised when the requested changelog section occurs more than once."""


class ArtifactError(ReleaseError):
    """Raised when built artifacts cannot be identified exactly."""


class RegistryConflictError(ReleaseError):
    """Raised when a registry contains a conflicting immutable artifact."""


class TagConflictError(ReleaseError):
    """Raised when an existing tag is not the requested annotated tag."""


class ReleasePreparationError(ReleaseError):
    """Raised after release preparation fails with its evidence preserved."""


class RegistryTransportError(ReleaseError):
    """Raised when a registry cannot return an authoritative response."""


class RegistryResponseError(ReleaseError):
    """Raised when a registry returns an unsupported response shape."""


class GitHubReleaseConflictError(ReleaseError):
    """Raised when an existing GitHub Release differs from local evidence."""


class BackmergeConflictError(ReleaseError):
    """Raised when existing backmerge state is not a verified safe rerun."""


@dataclass(frozen=True)
class ReleaseVersion:
    """A supported Python release version with a Cargo mapping."""

    major: int
    minor: int
    patch: int
    phase: str | None = None
    prerelease_number: int | None = None

    @classmethod
    def from_python(cls, value: str) -> ReleaseVersion:
        """Parse the supported PEP 440 release-version subset."""
        match = _VERSION_PATTERN.fullmatch(value.strip())
        if match is None:
            raise InvalidVersionError(f"Unsupported version {value!r}; use X.Y.Z or X.Y.ZaN, X.Y.ZbN, or X.Y.ZrcN.")
        phase = match.group("phase")
        number = match.group("number")
        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
            phase=phase,
            prerelease_number=None if number is None else int(number),
        )

    @classmethod
    def from_cargo(cls, value: str) -> ReleaseVersion:
        """Parse a Cargo SemVer version in the supported mapping."""
        match = _CARGO_VERSION_PATTERN.fullmatch(value.strip())
        if match is None:
            raise InvalidVersionError(f"Unsupported Cargo version {value!r}.")
        cargo_phase = match.group("phase")
        phase = {"alpha": "a", "beta": "b", "rc": "rc"}.get(cargo_phase)
        number = match.group("number")
        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
            phase=phase,
            prerelease_number=None if number is None else int(number),
        )

    @property
    def python(self) -> str:
        """Return the canonical PEP 440 spelling."""
        suffix = "" if self.phase is None else f"{self.phase}{self.prerelease_number}"
        return f"{self.major}.{self.minor}.{self.patch}{suffix}"

    @property
    def cargo(self) -> str:
        """Return the corresponding Cargo SemVer spelling."""
        if self.phase is None:
            suffix = ""
        else:
            cargo_phase = {"a": "alpha", "b": "beta", "rc": "rc"}[self.phase]
            suffix = f"-{cargo_phase}.{self.prerelease_number}"
        return f"{self.major}.{self.minor}.{self.patch}{suffix}"

    def ordering_key(self) -> tuple[int, int, int, int, int]:
        """Return a comparison key with stable releases after prereleases."""
        phase_order = {"a": 0, "b": 1, "rc": 2, None: 3}
        return (
            self.major,
            self.minor,
            self.patch,
            phase_order[self.phase],
            -1 if self.prerelease_number is None else self.prerelease_number,
        )


@dataclass(frozen=True)
class WheelTags:
    """Compatibility tags encoded in a wheel filename."""

    python: str
    abi: str
    platform: str


@dataclass(frozen=True)
class ArtifactIdentity:
    """Content and package identity for one release artifact."""

    filename: str
    kind: str
    distribution: str
    canonical_distribution: str
    version: str
    size: int
    sha256: str
    wheel_tags: WheelTags | None


class RegistryArtifactState(str, Enum):
    """Comparison state for an artifact at a package registry."""

    MISSING = "missing"
    IDENTICAL = "identical"
    CONFLICT = "conflict"


class BuildKind(str, Enum):
    """Artifact kinds supported by one release build invocation."""

    WHEEL = "wheel"
    SDIST = "sdist"
    ALL = "all"


class ReleaseEventKind(str, Enum):
    """Explicit workflow events that can establish release identity."""

    PULL_REQUEST = "pull_request"
    WORKFLOW_DISPATCH = "workflow_dispatch"
    PUSH = "push"


class CrateVersionState(str, Enum):
    """Comparison state for one immutable crates.io version."""

    MISSING = "missing"
    IDENTICAL = "identical"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class ReleaseIdentity:
    """Validated version, tag, and full source commit for a release run."""

    version: ReleaseVersion
    tag: str
    commit: str


def _run_checked(command: Sequence[str], repo: Path = REPO_ROOT) -> str:
    """Run a subprocess and return stdout, failing on every nonzero status."""
    result = subprocess.run(
        list(command),
        cwd=repo,
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def _git(*arguments: str, repo: Path = REPO_ROOT) -> str:
    """Run a checked Git command."""
    return _run_checked(("git", *arguments), repo)


def read_project_version(repo: Path = REPO_ROOT) -> ReleaseVersion:
    """Read the authoritative version from pyproject.toml."""
    pyproject = (repo / PYPROJECT_PATH).read_text(encoding="utf-8")
    project_match = _PROJECT_VERSION_PATTERN.search(pyproject)
    if project_match is None:
        raise VersionMismatchError("pyproject.toml has no [project] table.")
    version_match = _TOML_VERSION_PATTERN.search(project_match.group("body"))
    if version_match is None:
        raise VersionMismatchError("pyproject.toml [project] has no version.")
    return ReleaseVersion.from_python(version_match.group("version"))


def _replace_cargo_workspace_version(repo: Path, version: ReleaseVersion) -> None:
    """Apply the explicit PEP 440-to-Cargo prerelease mapping."""
    manifest_path = repo / RUST_MANIFEST_PATH
    if not manifest_path.exists():
        return
    source = manifest_path.read_text(encoding="utf-8")
    workspace_match = re.search(r"(?ms)^\[workspace\.package\]\s*$.*?(?=^\[|\Z)", source)
    package_match = re.search(r"(?ms)^\[package\]\s*$.*?(?=^\[|\Z)", source)
    table_match = workspace_match if workspace_match is not None else package_match
    if table_match is None:
        raise VersionMismatchError("rust/Cargo.toml has no workspace.package or package version table.")
    table = table_match.group(0)
    replaced, count = _TOML_VERSION_PATTERN.subn(f'version = "{version.cargo}"', table, count=1)
    if count != 1:
        raise VersionMismatchError("rust/Cargo.toml release table has no version.")
    updated = source[: table_match.start()] + replaced + source[table_match.end() :]
    manifest_path.write_text(updated, encoding="utf-8")


def extract_release_notes(changelog: str, version: ReleaseVersion) -> str:
    """Return exactly one committed level-two changelog section body."""
    headings = list(_CHANGELOG_HEADING_PATTERN.finditer(changelog))
    matches = [heading for heading in headings if heading.group("version") == version.python]
    if not matches:
        raise MissingChangelogSectionError(f"Changelog has no section for v{version.python}.")
    if len(matches) > 1:
        raise DuplicateChangelogSectionError(f"Changelog has {len(matches)} sections for v{version.python}.")
    heading = matches[0]
    next_heading = _LEVEL_TWO_HEADING_PATTERN.search(changelog, heading.end())
    end = len(changelog) if next_heading is None else next_heading.start()
    lines = changelog[heading.end() : end].splitlines(keepends=True)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "".join(lines).rstrip("\r\n")


def _require_clean_synchronized_develop(repo: Path) -> str:
    """Return develop HEAD after proving the release transaction is safe."""
    if _git("status", "--porcelain", repo=repo):
        raise RepositoryStateError("The working tree must be clean before preparing a release.")
    current_branch = _git("branch", "--show-current", repo=repo)
    if current_branch != "develop":
        raise RepositoryStateError(f"Prepare releases from develop, not {current_branch or 'detached HEAD'}.")
    _git("fetch", "--prune", "origin", repo=repo)
    develop = _git("rev-parse", "develop", repo=repo)
    remote_develop = _git("rev-parse", "origin/develop", repo=repo)
    main = _git("rev-parse", "main", repo=repo)
    remote_main = _git("rev-parse", "origin/main", repo=repo)
    if develop != remote_develop:
        raise RepositoryStateError("Local develop must equal origin/develop.")
    if main != remote_main:
        raise RepositoryStateError("Local main must equal origin/main.")
    try:
        _git("merge-base", "--is-ancestor", "main", "develop", repo=repo)
    except subprocess.CalledProcessError as error:
        raise RepositoryStateError("develop must contain main before preparing a release.") from error
    return develop


def _next_version(request: str, repo: Path) -> ReleaseVersion:
    """Resolve an increment keyword or validate an explicit target version."""
    normalized = request.upper()
    current = read_project_version(repo)
    if normalized in {"MAJOR", "MINOR", "PATCH"}:
        output = _run_checked(
            (
                "uv",
                "run",
                "cz",
                "bump",
                "--get-next",
                "--dry-run",
                "--yes",
                "--increment",
                normalized,
            ),
            repo,
        )
        candidates = re.findall(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?", output)
        if not candidates:
            raise InvalidVersionError(f"Commitizen did not return a next version: {output!r}.")
        return ReleaseVersion.from_python(candidates[-1])
    target = ReleaseVersion.from_python(request)
    if target.ordering_key() <= current.ordering_key():
        raise InvalidVersionError(f"Target {target.python} must be newer than {current.python}.")
    return target


def _bump_with_commitizen(request: str, repo: Path) -> None:
    """Update authoritative files and the changelog without committing or tagging."""
    command = ["uv", "run", "cz", "bump", "--yes", "--files-only", "--changelog"]
    normalized = request.upper()
    if normalized in {"MAJOR", "MINOR", "PATCH"}:
        command.extend(("--increment", normalized))
    else:
        command.append(ReleaseVersion.from_python(request).python)
    _run_checked(command, repo)


def prepare_release(request: str, repo: Path = REPO_ROOT) -> ReleaseVersion:
    """Create a release branch while preserving all failure evidence."""
    starting_commit = _require_clean_synchronized_develop(repo)
    target = _next_version(request, repo)
    branch = f"release/{target.python}"
    try:
        _git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", repo=repo)
    except subprocess.CalledProcessError:
        pass
    else:
        raise RepositoryStateError(f"Release branch {branch} already exists.")
    try:
        _git("show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}", repo=repo)
    except subprocess.CalledProcessError:
        pass
    else:
        raise RepositoryStateError(f"Remote release branch origin/{branch} already exists.")
    _git("checkout", "-b", branch, starting_commit, repo=repo)
    try:
        _bump_with_commitizen(request, repo)
        actual = read_project_version(repo)
        if actual != target:
            raise VersionMismatchError(f"Commitizen produced {actual.python}, expected {target.python}.")
        _replace_cargo_workspace_version(repo, target)
        _run_checked(("uv", "lock"), repo)
        if (repo / RUST_MANIFEST_PATH).exists():
            _run_checked(
                ("cargo", "check", "--workspace", "--manifest-path", str(RUST_MANIFEST_PATH)),
                repo,
            )
        owned_paths = [str(path) for path in _RELEASE_OWNED_PATHS if (repo / path).exists()]
        _git("add", "--", *owned_paths, repo=repo)
        _git("commit", "-m", f"chore(release): prepare v{target.python}", repo=repo)
        validate_release(repo)
    except Exception as error:
        raise ReleasePreparationError(
            f"Release preparation failed on {branch}. The branch and all generated evidence were preserved; "
            "inspect the working tree, repair it in place, or delete the branch manually after saving anything needed."
        ) from error
    return target


def _validate_rust_versions(version: ReleaseVersion, repo: Path) -> None:
    """Validate workspace member versions and Cargo.lock through Cargo metadata."""
    if not (repo / RUST_MANIFEST_PATH).exists():
        return
    output = _run_checked(
        (
            "cargo",
            "metadata",
            "--locked",
            "--no-deps",
            "--format-version",
            "1",
            "--manifest-path",
            str(RUST_MANIFEST_PATH),
        ),
        repo,
    )
    metadata = json.loads(output)
    workspace_members = set(metadata["workspace_members"])
    packages = [package for package in metadata["packages"] if package["id"] in workspace_members]
    mismatches = [
        f"{package['name']}={package['version']}"
        for package in packages
        if ReleaseVersion.from_cargo(package["version"]) != version
    ]
    if mismatches:
        raise VersionMismatchError(f"Cargo workspace versions do not match {version.cargo}: {', '.join(mismatches)}.")
    _run_checked(
        (
            "cargo",
            "package",
            "--locked",
            "--package",
            RUST_PROJECT_NAME,
            "--manifest-path",
            str(RUST_MANIFEST_PATH),
        ),
        repo,
    )
    if PUBLISH_RUST_PACKAGE:
        _run_checked(
            (
                "cargo",
                "publish",
                "--dry-run",
                "--locked",
                "--package",
                RUST_PROJECT_NAME,
                "--manifest-path",
                str(RUST_MANIFEST_PATH),
            ),
            repo,
        )


def _read_uv_lock_version(repo: Path) -> ReleaseVersion:
    """Read the editable root package version recorded by uv."""
    lock_path = repo / UV_LOCK_PATH
    if not lock_path.exists():
        raise VersionMismatchError("uv.lock is missing.")
    blocks = re.split(r"(?m)^\[\[package\]\][ \t]*\r?\n", lock_path.read_text(encoding="utf-8"))[1:]
    matches: list[ReleaseVersion] = []
    for block in blocks:
        name_match = re.search(r'(?m)^name[ \t]*=[ \t]*"(?P<name>[^"]+)"[ \t]*$', block)
        source_match = re.search(r'(?m)^source[ \t]*=[ \t]*\{[^\r\n]*editable[ \t]*=[ \t]*"\."', block)
        if name_match is None or source_match is None:
            continue
        if _normalize_distribution(name_match.group("name")) != _normalize_distribution(PROJECT_NAME):
            continue
        version_match = _TOML_VERSION_PATTERN.search(block)
        if version_match is None:
            raise VersionMismatchError("The uv.lock root project entry has no version.")
        matches.append(ReleaseVersion.from_python(version_match.group("version")))
    if len(matches) != 1:
        raise VersionMismatchError(f"Expected one editable {PROJECT_NAME} entry in uv.lock, found {len(matches)}.")
    return matches[0]


def validate_release(repo: Path = REPO_ROOT) -> ReleaseVersion:
    """Validate release version, lockfiles, and changelog."""
    version = read_project_version(repo)
    _run_checked(("uv", "lock", "--check"), repo)
    lock_version = _read_uv_lock_version(repo)
    if lock_version != version:
        raise VersionMismatchError(f"uv.lock has {lock_version.python}, expected {version.python}.")
    commitizen_version = ReleaseVersion.from_python(_run_checked(("uv", "run", "cz", "version", "-p"), repo))
    if commitizen_version != version:
        raise VersionMismatchError(f"Commitizen reports {commitizen_version.python}, expected {version.python}.")
    extract_release_notes((repo / CHANGELOG_PATH).read_text(encoding="utf-8"), version)
    _validate_rust_versions(version, repo)
    return version


def _pull_request_release_version(branch: str | None, tag: str | None) -> ReleaseVersion:
    """Validate pull-request fields and return the branch version."""
    if branch is None or not branch.startswith("release/"):
        raise RepositoryStateError("A pull_request release requires --branch release/<version>.")
    if tag is not None:
        raise RepositoryStateError("A pull_request release derives its tag; do not pass --tag.")
    return ReleaseVersion.from_python(branch.removeprefix("release/"))


def _push_release_version(tag: str | None, branch: str | None, commit: str, repo: Path) -> ReleaseVersion:
    """Validate tag-push fields and return the tag version."""
    if tag is None or not tag.startswith("v"):
        raise RepositoryStateError("A push recovery requires --tag v<version>.")
    if branch is not None:
        raise RepositoryStateError("A push recovery does not accept --branch.")
    tagged_commit = _git("rev-parse", f"refs/tags/{tag}^" + "{commit}", repo=repo)
    if tagged_commit != commit:
        raise TagConflictError(f"Tag {tag} targets {tagged_commit}, not {commit}.")
    return ReleaseVersion.from_python(tag.removeprefix("v"))


def _manual_release_version(version_text: str | None, branch: str | None) -> ReleaseVersion:
    """Validate manual-dispatch fields and return the requested version."""
    if version_text is None:
        raise RepositoryStateError("A workflow_dispatch release requires --version.")
    event_version = ReleaseVersion.from_python(version_text.removeprefix("v"))
    if branch is None:
        return event_version
    if not branch.startswith("release/"):
        raise RepositoryStateError("A workflow_dispatch branch must use release/<version>.")
    branch_version = ReleaseVersion.from_python(branch.removeprefix("release/"))
    if branch_version != event_version:
        raise VersionMismatchError(f"Branch {branch!r} does not match {event_version.python}.")
    return event_version


def _event_release_version(
    event_kind: ReleaseEventKind,
    branch: str | None,
    version_text: str | None,
    tag: str | None,
    commit: str,
    repo: Path,
) -> ReleaseVersion:
    """Validate event-specific fields and return their release version."""
    if event_kind is ReleaseEventKind.PULL_REQUEST:
        return _pull_request_release_version(branch, tag)
    if event_kind is ReleaseEventKind.PUSH:
        return _push_release_version(tag, branch, commit, repo)
    return _manual_release_version(version_text, branch)


def resolve_release_identity(
    event_kind: ReleaseEventKind,
    commit: str,
    *,
    branch: str | None = None,
    version_text: str | None = None,
    tag: str | None = None,
    repo: Path = REPO_ROOT,
) -> ReleaseIdentity:
    """Resolve explicit event inputs into one validated release identity."""
    resolved_commit = _git("rev-parse", f"{commit}^" + "{commit}", repo=repo)
    if _git("rev-parse", "HEAD", repo=repo) != resolved_commit:
        raise RepositoryStateError(f"HEAD is not the requested release commit {resolved_commit}.")
    event_version = _event_release_version(event_kind, branch, version_text, tag, resolved_commit, repo)

    if version_text is not None and ReleaseVersion.from_python(version_text.removeprefix("v")) != event_version:
        raise VersionMismatchError(f"Version {version_text!r} does not match event version {event_version.python}.")
    expected_tag = f"v{event_version.python}"
    if tag is not None and tag != expected_tag:
        raise VersionMismatchError(f"Tag {tag!r} does not match {expected_tag}.")
    if tag is not None and event_kind is not ReleaseEventKind.PUSH:
        tagged_commit = _git("rev-parse", f"refs/tags/{tag}^" + "{commit}", repo=repo)
        if tagged_commit != resolved_commit:
            raise TagConflictError(f"Tag {tag} targets {tagged_commit}, not {resolved_commit}.")
    project_version = read_project_version(repo)
    if project_version != event_version:
        raise VersionMismatchError(
            f"Event identifies {event_version.python}, but pyproject.toml contains {project_version.python}."
        )
    return ReleaseIdentity(version=event_version, tag=expected_tag, commit=resolved_commit)


def write_release_identity(identity: ReleaseIdentity, output_path: Path) -> None:
    """Write release identity fields to an explicit workflow output file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        f"version={identity.version.python}\n"
        f"cargo_version={identity.version.cargo}\n"
        f"commit={identity.commit}\n"
        f"tag={identity.tag}\n",
        encoding="utf-8",
    )


def _normalize_distribution(value: str) -> str:
    """Normalize a Python distribution name for identity comparison."""
    return re.sub(r"[-_.]+", "-", value).lower()


def _metadata_identity(path: Path) -> tuple[str, str]:
    """Read Name and Version from an artifact's core metadata."""
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(metadata_names) != 1:
                raise ArtifactError(f"{path.name} contains {len(metadata_names)} METADATA files.")
            metadata_text = archive.read(metadata_names[0]).decode("utf-8")
    elif path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            metadata_members = [
                member
                for member in archive.getmembers()
                if member.name.endswith("/PKG-INFO") and member.name.count("/") == 1
            ]
            if len(metadata_members) != 1:
                raise ArtifactError(f"{path.name} contains {len(metadata_members)} PKG-INFO files.")
            metadata_file = archive.extractfile(metadata_members[0])
            if metadata_file is None:
                raise ArtifactError(f"Cannot read metadata from {path.name}.")
            metadata_text = metadata_file.read().decode("utf-8")
    else:
        raise ArtifactError(f"Unsupported release artifact {path.name}.")
    metadata = Parser().parsestr(metadata_text)
    distribution = metadata.get("Name")
    version = metadata.get("Version")
    if distribution is None or version is None:
        raise ArtifactError(f"{path.name} metadata lacks Name or Version.")
    return distribution, ReleaseVersion.from_python(version).python


def identify_artifact(path: Path) -> ArtifactIdentity:
    """Create an immutable identity record for one Python artifact."""
    distribution, version = _metadata_identity(path)
    wheel_tags = None
    if path.suffix == ".whl":
        match = _WHEEL_FILENAME_PATTERN.fullmatch(path.name)
        if match is None:
            raise ArtifactError(f"Invalid wheel filename {path.name}.")
        if _normalize_distribution(match.group("distribution")) != _normalize_distribution(distribution):
            raise ArtifactError(f"{path.name} distribution disagrees with its metadata.")
        if ReleaseVersion.from_python(match.group("version")).python != version:
            raise ArtifactError(f"{path.name} version disagrees with its metadata.")
        wheel_tags = WheelTags(
            python=match.group("python_tag"),
            abi=match.group("abi_tag"),
            platform=match.group("platform_tag"),
        )
    else:
        suffix = f"-{version}.tar.gz"
        if not path.name.endswith(suffix):
            raise ArtifactError(f"{path.name} version disagrees with its metadata.")
        filename_distribution = path.name[: -len(suffix)]
        if _normalize_distribution(filename_distribution) != _normalize_distribution(distribution):
            raise ArtifactError(f"{path.name} distribution disagrees with its metadata.")
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for block in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(block)
    return ArtifactIdentity(
        filename=path.name,
        kind="wheel" if path.suffix == ".whl" else "sdist",
        distribution=distribution,
        canonical_distribution=_normalize_distribution(distribution),
        version=version,
        size=path.stat().st_size,
        sha256=digest.hexdigest(),
        wheel_tags=wheel_tags,
    )


def write_artifact_manifest(artifact_dir: Path, manifest_path: Path, repo: Path = REPO_ROOT) -> list[ArtifactIdentity]:
    """Write deterministic identities for all aggregated Python artifacts."""
    paths = sorted(
        (
            path
            for path in artifact_dir.iterdir()
            if path.is_file() and (path.suffix == ".whl" or path.name.endswith(".tar.gz"))
        ),
        key=lambda path: path.name,
    )
    if not paths:
        raise ArtifactError(f"No Python artifacts found in {artifact_dir}.")
    identities = [identify_artifact(path) for path in paths]
    distributions = {_normalize_distribution(identity.distribution) for identity in identities}
    versions = {identity.version for identity in identities}
    if len(distributions) != 1 or len(versions) != 1:
        raise ArtifactError("Aggregated artifacts do not share one distribution and version.")
    expected_version = read_project_version(repo).python
    if distributions != {_normalize_distribution(PROJECT_NAME)} or versions != {expected_version}:
        raise ArtifactError(
            f"Artifacts must identify {PROJECT_NAME} {expected_version}, got "
            f"{identities[0].distribution} {identities[0].version}."
        )
    payload = {
        "schema_version": 1,
        "distribution": identities[0].distribution,
        "canonical_distribution": _normalize_distribution(identities[0].distribution),
        "version": identities[0].version,
        "source_commit": _git("rev-parse", "HEAD", repo=repo),
        "files": [asdict(identity) for identity in identities],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return identities


def build_release(
    out_dir: Path,
    kind: BuildKind,
    target: str | None = None,
    interpreter: str | None = None,
    repo: Path = REPO_ROOT,
) -> list[ArtifactIdentity]:
    """Build exactly the requested release artifact kind or kinds."""
    if kind is BuildKind.SDIST and (target is not None or interpreter is not None):
        raise ArtifactError("An sdist build does not accept --target or --interpreter.")
    out_dir = (repo / out_dir).resolve() if not out_dir.is_absolute() else out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    is_maturin = "[tool.maturin]" in (repo / PYPROJECT_PATH).read_text(encoding="utf-8")
    if not is_maturin and target is not None:
        raise ArtifactError("--target is available only for Maturin builds.")
    for path in out_dir.iterdir():
        if path.is_file() and (
            path.suffix == ".whl" or path.name.endswith(".tar.gz") or path.name == ARTIFACT_MANIFEST_NAME
        ):
            path.unlink()
    if is_maturin:
        _build_maturin_release(out_dir, kind, target, interpreter, repo)
    else:
        _build_python_release(out_dir, kind, target, interpreter, repo)
    cell_manifest = out_dir / ARTIFACT_MANIFEST_NAME
    identities = write_artifact_manifest(out_dir, cell_manifest, repo)
    cell_manifest.unlink()
    for wheel in sorted(out_dir.glob("*.whl")):
        _smoke_test_wheel(wheel, repo)
    return identities


def _build_maturin_release(
    out_dir: Path,
    kind: BuildKind,
    target: str | None,
    interpreter: str | None,
    repo: Path,
) -> None:
    """Build Maturin artifacts for one explicit build kind."""
    if kind in {BuildKind.WHEEL, BuildKind.ALL}:
        command = ["uv", "run", "--locked", "maturin", "build", "--release", "--out", str(out_dir)]
        if target is not None:
            command.extend(("--target", target))
        if interpreter is not None:
            command.extend(("--interpreter", interpreter))
        _run_checked(command, repo)
    if kind in {BuildKind.SDIST, BuildKind.ALL}:
        _run_checked(("uv", "run", "--locked", "maturin", "sdist", "--out", str(out_dir)), repo)


def _build_python_release(
    out_dir: Path,
    kind: BuildKind,
    target: str | None,
    interpreter: str | None,
    repo: Path,
) -> None:
    """Build pure-Python artifacts for one explicit build kind."""
    if target is not None:
        raise ArtifactError("--target is available only for Maturin builds.")
    if kind in {BuildKind.WHEEL, BuildKind.ALL}:
        command = ["uv", "build", "--wheel", "--out-dir", str(out_dir)]
        if interpreter is not None:
            command.extend(("--python", interpreter))
        _run_checked(command, repo)
    if kind in {BuildKind.SDIST, BuildKind.ALL}:
        _run_checked(("uv", "build", "--sdist", "--out-dir", str(out_dir)), repo)


def _smoke_test_import(installed_path: Path, repo: Path) -> None:
    """Import the installed package and exercise its native sample when present."""
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(installed_path)!r}); "
        f"import {PACKAGE_NAME} as package; "
        "sample = getattr(package, 'sum_as_string', None); "
        "result = None if sample is None else sample(1, 2); "
        "(_ for _ in ()).throw(RuntimeError('native smoke test failed')) if result not in (None, '3') else None"
    )
    _run_checked((sys.executable, "-c", code), repo)


def _smoke_test_wheel(wheel: Path, repo: Path) -> None:
    """Install one host-cell wheel without dependencies and import it."""
    with tempfile.TemporaryDirectory(prefix="release-wheel-") as temporary:
        installed_path = Path(temporary) / "installed"
        _run_checked(
            (
                "uv",
                "pip",
                "install",
                "--python",
                sys.executable,
                "--target",
                str(installed_path),
                "--no-deps",
                str(wheel),
            ),
            repo,
        )
        _smoke_test_import(installed_path, repo)


def _parse_manifest_identity(item: object) -> ArtifactIdentity:
    """Parse and validate one untrusted manifest file entry."""
    if not isinstance(item, dict):
        raise ArtifactError("Artifact manifest file entries must be objects.")
    try:
        raw_tags = item.get("wheel_tags")
        tags = None
        if raw_tags is not None:
            if not isinstance(raw_tags, dict):
                raise ArtifactError("wheel_tags must be an object or null.")
            tags = WheelTags(
                python=str(raw_tags["python"]),
                abi=str(raw_tags["abi"]),
                platform=str(raw_tags["platform"]),
            )
        raw_filename = item["filename"]
        if not isinstance(raw_filename, str):
            raise ArtifactError("Artifact manifest filename must be a string.")
        filename = raw_filename
        if (
            not filename
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or ":" in filename
            or Path(filename).is_absolute()
            or Path(filename).name != filename
        ):
            raise ArtifactError(f"Artifact manifest filename {filename!r} must be a plain filename.")
        return ArtifactIdentity(
            filename=filename,
            kind=str(item["kind"]),
            distribution=str(item["distribution"]),
            canonical_distribution=str(item["canonical_distribution"]),
            version=ReleaseVersion.from_python(str(item["version"])).python,
            size=int(item["size"]),
            sha256=str(item["sha256"]),
            wheel_tags=tags,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ArtifactError("Artifact manifest contains an invalid file entry.") from error


def _validate_manifest_identity(identity: ArtifactIdentity, distribution: str, version: str) -> None:
    """Require one manifest entry to agree with the manifest envelope."""
    canonical_distribution = _normalize_distribution(distribution)
    is_wheel = identity.kind == "wheel"
    if (
        identity.distribution != distribution
        or identity.canonical_distribution != canonical_distribution
        or identity.version != version
        or identity.kind not in {"wheel", "sdist"}
        or is_wheel != (identity.wheel_tags is not None)
        or is_wheel != identity.filename.endswith(".whl")
        or (not is_wheel) != identity.filename.endswith(".tar.gz")
        or identity.size <= 0
        or re.fullmatch(r"[0-9a-f]{64}", identity.sha256) is None
    ):
        raise ArtifactError(f"Manifest identity for {identity.filename!r} is inconsistent.")


def _manifest_string(payload: object, field: str) -> str:
    """Read one required string from an untrusted JSON object."""
    if not isinstance(payload, dict):
        raise ArtifactError("Artifact manifest must be a JSON object.")
    value = payload.get(field)
    if not isinstance(value, str):
        raise ArtifactError(f"Artifact manifest field {field!r} must be a string.")
    return value


def _load_artifact_manifest(path: Path) -> tuple[str, str, str, list[ArtifactIdentity]]:
    """Validate an artifact-manifest JSON boundary."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ArtifactError("Artifact manifest must be a JSON object.")
    if payload.get("schema_version") != 1:
        raise ArtifactError("Unsupported artifact manifest schema.")
    distribution = _manifest_string(payload, "distribution")
    manifest_canonical_distribution = _manifest_string(payload, "canonical_distribution")
    version_value = _manifest_string(payload, "version")
    source_commit = _manifest_string(payload, "source_commit")
    files = payload.get("files")
    if not isinstance(files, list):
        raise ArtifactError("Artifact manifest files must be a list.")
    version = ReleaseVersion.from_python(version_value).python
    if manifest_canonical_distribution != _normalize_distribution(distribution):
        raise ArtifactError("Artifact manifest canonical distribution is inconsistent.")
    if re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        raise ArtifactError("Artifact manifest source_commit must be a full Git commit ID.")
    identities = [_parse_manifest_identity(item) for item in files]
    filenames = [identity.filename for identity in identities]
    if not identities or len(filenames) != len(set(filenames)):
        raise ArtifactError("Artifact manifest must contain unique file entries.")
    for identity in identities:
        _validate_manifest_identity(identity, distribution, version)
    return distribution, ReleaseVersion.from_python(version).python, source_commit, identities


def _fetch_registry_files(index: str, distribution: str, version: str) -> dict[str, object] | None:
    """Fetch registry release JSON and return files keyed by filename."""
    host = "test.pypi.org" if index == "testpypi" else "pypi.org"
    url = f"https://{host}/pypi/{urllib.parse.quote(distribution)}/{urllib.parse.quote(version)}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    if not isinstance(payload, dict):
        raise ArtifactError("Registry response must be a JSON object.")
    urls = payload.get("urls", [])
    if not isinstance(urls, list):
        raise ArtifactError("Registry response has no files list.")
    return {str(item["filename"]): item for item in urls if isinstance(item, dict) and "filename" in item}


def compare_registry_artifacts(index: str, manifest_path: Path) -> dict[str, RegistryArtifactState]:
    """Compare local identities with immutable files already at a registry."""
    if index not in {"testpypi", "pypi"}:
        raise ArtifactError(f"Unsupported registry {index!r}.")
    distribution, version, _, identities = _load_artifact_manifest(manifest_path)
    remote = _fetch_registry_files(index, distribution, version)
    if remote is None:
        return {identity.filename: RegistryArtifactState.MISSING for identity in identities}
    result: dict[str, RegistryArtifactState] = {}
    for identity in identities:
        remote_identity = remote.get(identity.filename)
        if not isinstance(remote_identity, dict):
            result[identity.filename] = RegistryArtifactState.MISSING
        elif (
            remote_identity.get("size") == identity.size
            and remote_identity.get("digests", {}).get("sha256") == identity.sha256
        ):
            result[identity.filename] = RegistryArtifactState.IDENTICAL
        else:
            result[identity.filename] = RegistryArtifactState.CONFLICT
    expected_filenames = {identity.filename for identity in identities}
    for unexpected in sorted(set(remote) - expected_filenames):
        result[f"unexpected:{unexpected}"] = RegistryArtifactState.CONFLICT
    return result


def _download_and_verify_registry_artifacts(index: str, manifest_path: Path, repo: Path = REPO_ROOT) -> None:
    """Download exact registry files, re-identify them, and smoke-test a compatible wheel."""
    distribution, version, _, identities = _load_artifact_manifest(manifest_path)
    remote = _fetch_registry_files(index, distribution, version)
    if remote is None:
        raise ArtifactError(f"{index} has no release for {distribution} {version}.")
    with tempfile.TemporaryDirectory(prefix=f"release-{index}-") as temporary:
        download_dir = Path(temporary) / "downloads"
        download_dir.mkdir()
        for identity in identities:
            remote_identity = remote.get(identity.filename)
            if not isinstance(remote_identity, dict):
                raise ArtifactError(f"{index} is missing {identity.filename}.")
            url = remote_identity.get("url")
            if not isinstance(url, str) or urllib.parse.urlparse(url).scheme != "https":
                raise ArtifactError(f"{index} returned an invalid URL for {identity.filename}.")
            destination = download_dir / identity.filename
            with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
                destination.write_bytes(response.read())
            if identify_artifact(destination) != identity:
                raise RegistryConflictError(f"Downloaded {identity.filename} does not match its manifest.")
        installed_path = Path(temporary) / "installed"
        _run_checked(
            (
                "uv",
                "pip",
                "install",
                "--python",
                sys.executable,
                "--target",
                str(installed_path),
                "--no-deps",
                "--no-index",
                "--find-links",
                str(download_dir),
                "--only-binary",
                ":all:",
                f"{distribution}=={version}",
            ),
            repo,
        )
        _smoke_test_import(installed_path, repo)


def verify_release_index(index: str, manifest_path: Path, allow_missing: bool = False) -> None:
    """Reject registry conflicts and optionally permit not-yet-uploaded files."""
    states = compare_registry_artifacts(index, manifest_path)
    conflicts = [name for name, state in states.items() if state is RegistryArtifactState.CONFLICT]
    missing = [name for name, state in states.items() if state is RegistryArtifactState.MISSING]
    if conflicts:
        raise RegistryConflictError(f"Registry has conflicting immutable files: {', '.join(conflicts)}.")
    if missing and not allow_missing:
        raise ArtifactError(f"Registry is missing release files: {', '.join(missing)}.")
    if index == "testpypi" and not allow_missing:
        _download_and_verify_registry_artifacts(index, manifest_path)


def finalize_release(version_text: str, commit: str, repo: Path = REPO_ROOT) -> bool:
    """Create an annotated exact-commit tag, returning false for a safe no-op."""
    requested = ReleaseVersion.from_python(version_text)
    resolved_commit = _git("rev-parse", f"{commit}^" + "{commit}", repo=repo)
    if _git("rev-parse", "HEAD", repo=repo) != resolved_commit:
        raise RepositoryStateError("Finalize the release from the exact commit being tagged.")
    actual = validate_release(repo)
    if requested != actual:
        raise VersionMismatchError(f"Requested {requested.python}, checkout contains {actual.python}.")
    tag = f"v{requested.python}"
    try:
        object_type = _git("cat-file", "-t", f"refs/tags/{tag}", repo=repo)
    except subprocess.CalledProcessError:
        _git("tag", "--annotate", tag, resolved_commit, "--message", f"Release {tag}", repo=repo)
        return True
    if object_type != "tag":
        raise TagConflictError(f"Existing {tag} is not an annotated tag.")
    existing_commit = _git("rev-parse", f"refs/tags/{tag}^" + "{commit}", repo=repo)
    if existing_commit != resolved_commit:
        raise TagConflictError(f"Existing {tag} targets {existing_commit}, not {resolved_commit}.")
    return False


def inspect_crate_version(crate_name: str, version: ReleaseVersion, archive: Path) -> CrateVersionState:
    """Compare one local crate archive with the immutable crates.io version."""
    if not archive.is_file():
        raise ArtifactError(f"Crate archive does not exist: {archive}.")
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    url = (
        "https://crates.io/api/v1/crates/"
        f"{urllib.parse.quote(crate_name, safe='')}/{urllib.parse.quote(version.cargo, safe='')}"
    )
    request = urllib.request.Request(  # noqa: S310
        url,
        headers={"User-Agent": f"{PROJECT_NAME}-release-tool"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return CrateVersionState.MISSING
        raise RegistryTransportError(
            f"crates.io returned HTTP {error.code} for {crate_name} {version.cargo}."
        ) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RegistryTransportError(f"Could not query crates.io for {crate_name} {version.cargo}.") from error
    except json.JSONDecodeError as error:
        raise RegistryResponseError("crates.io returned malformed JSON.") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("version"), dict):
        raise RegistryResponseError("crates.io response has no version object.")
    remote_checksum = payload["version"].get("checksum")
    if not isinstance(remote_checksum, str) or re.fullmatch(r"[0-9a-f]{64}", remote_checksum) is None:
        raise RegistryResponseError("crates.io response has no valid version checksum.")
    if remote_checksum == checksum:
        return CrateVersionState.IDENTICAL
    return CrateVersionState.CONFLICT


def write_crate_state(state: CrateVersionState, output_path: Path) -> None:
    """Write one crates.io comparison state to an explicit output file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(f"state={state.value}\n", encoding="utf-8")


def _run_process(command: Sequence[str], repo: Path) -> subprocess.CompletedProcess[str]:
    """Run a subprocess while retaining its status and diagnostic streams."""
    return subprocess.run(
        list(command),
        cwd=repo,
        check=False,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _manifest_release_assets(
    manifest_path: Path,
    artifact_dir: Path,
    expected_commit: str,
) -> tuple[ReleaseVersion, dict[str, tuple[int, str]]]:
    """Validate local release assets against their immutable manifest."""
    _, version_text, source_commit, identities = _load_artifact_manifest(manifest_path)
    if source_commit != expected_commit:
        raise ArtifactError(f"Artifact manifest targets {source_commit}, not release commit {expected_commit}.")
    artifact_root = artifact_dir.resolve()
    expected: dict[str, tuple[int, str]] = {}
    for identity in identities:
        asset_path = (artifact_root / identity.filename).resolve()
        if asset_path.parent != artifact_root:
            raise ArtifactError(f"Release asset {identity.filename!r} escapes the artifact directory.")
        if not asset_path.is_file() or identify_artifact(asset_path) != identity:
            raise ArtifactError(f"Local release asset {identity.filename} does not match the manifest.")
        expected[identity.filename] = (identity.size, identity.sha256)
    return ReleaseVersion.from_python(version_text), expected


def _parse_github_release_assets(payload: dict[str, object]) -> dict[str, tuple[int, str]]:
    """Parse GitHub Release asset identity from an untrusted API response."""
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        raise GitHubReleaseConflictError("GitHub Release response has no assets list.")
    assets: dict[str, tuple[int, str]] = {}
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict):
            raise GitHubReleaseConflictError("GitHub Release contains an invalid asset entry.")
        name = raw_asset.get("name")
        size = raw_asset.get("size")
        digest = raw_asset.get("digest")
        if (
            not isinstance(name, str)
            or not isinstance(size, int)
            or not isinstance(digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        ):
            raise GitHubReleaseConflictError("GitHub Release asset identity is incomplete.")
        if name in assets:
            raise GitHubReleaseConflictError(f"GitHub Release contains duplicate asset {name!r}.")
        assets[name] = (size, digest.removeprefix("sha256:"))
    return assets


def ensure_immutable_github_release(
    tag: str,
    commit: str,
    title: str,
    notes_path: Path,
    manifest_path: Path,
    artifact_dir: Path,
    repo: Path = REPO_ROOT,
) -> bool:
    """Create an absent GitHub Release or verify an exactly identical one."""
    resolved_commit = _git("rev-parse", f"{commit}^" + "{commit}", repo=repo)
    if _git("cat-file", "-t", f"refs/tags/{tag}", repo=repo) != "tag":
        raise TagConflictError(f"Existing {tag} is not an annotated tag.")
    tagged_commit = _git("rev-parse", f"refs/tags/{tag}^" + "{commit}", repo=repo)
    if tagged_commit != resolved_commit:
        raise TagConflictError(f"Tag {tag} targets {tagged_commit}, not {resolved_commit}.")
    notes = notes_path.read_text(encoding="utf-8")
    release_version, expected_assets = _manifest_release_assets(manifest_path, artifact_dir, resolved_commit)
    if tag != f"v{release_version.python}":
        raise VersionMismatchError(f"Tag {tag!r} does not match artifact version {release_version.python}.")
    is_prerelease = release_version.phase is not None
    view = _run_process(("gh", "api", "repos/{owner}/{repo}/releases/tags/" + tag), repo)
    if view.returncode != 0:
        diagnostic = f"{view.stdout}\n{view.stderr}"
        if "HTTP 404" not in diagnostic and "Not Found" not in diagnostic:
            raise RegistryTransportError(f"Could not inspect GitHub Release {tag}: {view.stderr.strip()}")
        asset_paths = [str(artifact_dir / filename) for filename in sorted(expected_assets)]
        command = [
            "gh",
            "release",
            "create",
            tag,
            *asset_paths,
            "--title",
            title,
            "--notes-file",
            str(notes_path),
            "--verify-tag",
            "--target",
            resolved_commit,
        ]
        if is_prerelease:
            command.append("--prerelease")
        _run_checked(command, repo)
        return True
    try:
        payload = json.loads(view.stdout)
    except json.JSONDecodeError as error:
        raise GitHubReleaseConflictError("GitHub Release response is malformed JSON.") from error
    if not isinstance(payload, dict):
        raise GitHubReleaseConflictError("GitHub Release response must be an object.")
    actual_assets = _parse_github_release_assets(payload)
    if (
        payload.get("tag_name") != tag
        or payload.get("name") != title
        or payload.get("body") != notes
        or payload.get("draft") is not False
        or payload.get("prerelease") is not is_prerelease
        or actual_assets != expected_assets
    ):
        raise GitHubReleaseConflictError(
            f"Existing GitHub Release {tag} differs from the requested immutable title, notes, or assets."
        )
    return False


def _require_released_commit(version: ReleaseVersion, commit: str, repo: Path) -> tuple[str, str]:
    """Return an exact full commit and tag after validating their relationship."""
    resolved_commit = _git("rev-parse", f"{commit}^" + "{commit}", repo=repo)
    tag = f"v{version.python}"
    if _git("cat-file", "-t", f"refs/tags/{tag}", repo=repo) != "tag":
        raise TagConflictError(f"Existing {tag} is not an annotated tag.")
    tagged_commit = _git("rev-parse", f"refs/tags/{tag}^" + "{commit}", repo=repo)
    if tagged_commit != resolved_commit:
        raise TagConflictError(f"Tag {tag} targets {tagged_commit}, not {resolved_commit}.")
    return resolved_commit, tag


def _verify_existing_backmerge(branch: str, release_commit: str, repo: Path) -> bool:
    """Verify an existing merge branch and report whether its exact PR exists."""
    _git("fetch", "origin", branch, "develop", repo=repo)
    remote_tip = _git("rev-parse", f"origin/{branch}^" + "{commit}", repo=repo)
    parent_line = _git("rev-list", "--parents", "-n", "1", remote_tip, repo=repo).split()
    if len(parent_line) != 3 or parent_line[2] != release_commit:
        raise BackmergeConflictError(f"origin/{branch} is not the expected two-parent release merge.")
    try:
        _git("merge-base", "--is-ancestor", parent_line[1], "origin/develop", repo=repo)
    except subprocess.CalledProcessError as error:
        raise BackmergeConflictError(
            f"The first parent of origin/{branch} is not an ancestor of current origin/develop."
        ) from error
    try:
        merge_tree = _git("merge-tree", "--write-tree", parent_line[1], release_commit, repo=repo).splitlines()[0]
    except (subprocess.CalledProcessError, IndexError) as error:
        raise BackmergeConflictError(f"Could not reproduce the merge tree for origin/{branch}.") from error
    actual_tree = _git("rev-parse", f"{remote_tip}^" + "{tree}", repo=repo)
    if merge_tree != actual_tree:
        raise BackmergeConflictError(
            f"origin/{branch} has tree {actual_tree}, not deterministic merge tree {merge_tree}."
        )
    raw_prs = _run_checked(
        (
            "gh",
            "pr",
            "list",
            "--state",
            "all",
            "--head",
            branch,
            "--json",
            "number,state,headRefOid,baseRefName,headRefName",
        ),
        repo,
    )
    prs = json.loads(raw_prs)
    if isinstance(prs, list) and not prs:
        return False
    if (
        not isinstance(prs, list)
        or len(prs) != 1
        or not isinstance(prs[0], dict)
        or prs[0].get("state") != "OPEN"
        or prs[0].get("baseRefName") != "develop"
        or prs[0].get("headRefName") != branch
        or prs[0].get("headRefOid") != remote_tip
    ):
        raise BackmergeConflictError(f"origin/{branch} has mismatched or non-open pull-request state.")
    return True


def _create_backmerge_pull_request(branch: str, tag: str, repo: Path) -> None:
    """Open the one expected release backmerge pull request."""
    _run_checked(
        (
            "gh",
            "pr",
            "create",
            "--base",
            "develop",
            "--head",
            branch,
            "--title",
            f"chore: backmerge {tag} into develop",
            "--body",
            f"Backmerge the released main branch into develop after {tag}.",
        ),
        repo,
    )


def open_release_backmerge(version_text: str, commit: str, repo: Path = REPO_ROOT) -> bool:
    """Create a non-destructive release backmerge or verify its safe rerun."""
    version = ReleaseVersion.from_python(version_text.removeprefix("v"))
    release_commit, tag = _require_released_commit(version, commit, repo)
    branch = f"backmerge/{tag}"
    remote = _run_process(("git", "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}"), repo)
    if remote.returncode == 0:
        if not _verify_existing_backmerge(branch, release_commit, repo):
            _create_backmerge_pull_request(branch, tag, repo)
            return True
        return False
    if remote.returncode != 2:
        raise RepositoryStateError(f"Could not determine whether origin/{branch} exists: {remote.stderr.strip()}")
    try:
        _git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", repo=repo)
    except subprocess.CalledProcessError:
        pass
    else:
        raise BackmergeConflictError(f"Local branch {branch} already exists without a remote counterpart.")
    if _git("status", "--porcelain", repo=repo):
        raise RepositoryStateError("The working tree must be clean before creating a backmerge.")
    _git("fetch", "origin", "develop", repo=repo)
    _git("checkout", "-b", branch, "origin/develop", repo=repo)
    try:
        _git("merge", "--no-ff", "--no-edit", release_commit, repo=repo)
    except subprocess.CalledProcessError as error:
        raise BackmergeConflictError(
            f"Backmerge conflicts were preserved on {branch}; resolve them and continue manually."
        ) from error
    merge_line = _git("rev-list", "--parents", "-n", "1", "HEAD", repo=repo).split()
    if len(merge_line) != 3 or merge_line[2] != release_commit:
        raise BackmergeConflictError("Git did not create the expected two-parent backmerge commit.")
    _git("push", "origin", branch, repo=repo)
    _create_backmerge_pull_request(branch, tag, repo)
    return True


def _parser() -> argparse.ArgumentParser:
    """Build the release command parser."""
    parser = argparse.ArgumentParser(prog="release")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("version")
    commands.add_parser("validate")
    build = commands.add_parser("build")
    build.add_argument("--kind", required=True, choices=tuple(kind.value for kind in BuildKind))
    build.add_argument("--target")
    build.add_argument("--interpreter")
    build.add_argument("--out-dir", type=Path, default=Path("dist"))
    notes = commands.add_parser("extract-notes")
    notes.add_argument("version")
    notes.add_argument("path", nargs="?", type=Path, default=Path("release-notes.md"))
    finalize = commands.add_parser("finalize")
    finalize.add_argument("version")
    finalize.add_argument("commit")
    manifest = commands.add_parser("manifest")
    manifest.add_argument("artifact_dir", type=Path)
    manifest.add_argument("path", type=Path)
    verify = commands.add_parser("verify-index")
    verify.add_argument("index", choices=("testpypi", "pypi"))
    verify.add_argument("manifest", type=Path)
    verify.add_argument("--allow-missing", action="store_true")
    identity = commands.add_parser("resolve-identity")
    identity.add_argument("--event-kind", required=True, choices=tuple(kind.value for kind in ReleaseEventKind))
    identity.add_argument("--commit", required=True)
    identity.add_argument("--branch")
    identity.add_argument("--version")
    identity.add_argument("--tag")
    identity.add_argument("--output", required=True, type=Path)
    crate = commands.add_parser("crate-state")
    crate.add_argument("crate_name")
    crate.add_argument("version")
    crate.add_argument("archive", type=Path)
    crate.add_argument("output", type=Path)
    github_release = commands.add_parser("github-release")
    github_release.add_argument("tag")
    github_release.add_argument("commit")
    github_release.add_argument("title")
    github_release.add_argument("notes", type=Path)
    github_release.add_argument("manifest", type=Path)
    github_release.add_argument("artifact_dir", type=Path)
    backmerge = commands.add_parser("backmerge")
    backmerge.add_argument("version")
    backmerge.add_argument("commit")
    return parser


def _run_external_release_command(args: argparse.Namespace) -> None:
    """Dispatch commands that integrate with release orchestration boundaries."""
    if args.command == "resolve-identity":
        identity = resolve_release_identity(
            ReleaseEventKind(args.event_kind),
            args.commit,
            branch=args.branch,
            version_text=args.version,
            tag=args.tag,
        )
        write_release_identity(identity, args.output)
    elif args.command == "crate-state":
        state = inspect_crate_version(
            args.crate_name,
            ReleaseVersion.from_python(args.version.removeprefix("v")),
            args.archive,
        )
        write_crate_state(state, args.output)
        if state is CrateVersionState.CONFLICT:
            raise RegistryConflictError(
                f"crates.io already contains a conflicting immutable {args.crate_name} {args.version} archive."
            )
    elif args.command == "github-release":
        ensure_immutable_github_release(
            args.tag,
            args.commit,
            args.title,
            args.notes,
            args.manifest,
            args.artifact_dir,
        )
    elif args.command == "backmerge":
        open_release_backmerge(args.version, args.commit)
    else:
        raise AssertionError(f"Unhandled release command {args.command!r}.")


def main(arguments: Sequence[str] | None = None) -> int:
    """Run one release operation with a concise intentional error boundary."""
    args = _parser().parse_args(arguments)
    try:
        if args.command == "prepare":
            prepare_release(args.version)
        elif args.command == "validate":
            validate_release()
        elif args.command == "build":
            build_release(args.out_dir, BuildKind(args.kind), args.target, args.interpreter)
        elif args.command == "extract-notes":
            version = ReleaseVersion.from_python(args.version)
            notes = extract_release_notes(CHANGELOG_PATH.read_text(encoding="utf-8"), version)
            args.path.write_text(notes if notes.endswith(("\n", "\r")) else notes + "\n", encoding="utf-8")
        elif args.command == "finalize":
            finalize_release(args.version, args.commit)
        elif args.command == "manifest":
            write_artifact_manifest(args.artifact_dir, args.path)
        elif args.command == "verify-index":
            verify_release_index(args.index, args.manifest, args.allow_missing)
        else:
            _run_external_release_command(args)
    except (ReleaseError, subprocess.CalledProcessError, OSError, json.JSONDecodeError) as error:
        print(f"release: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
