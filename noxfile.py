"""Noxfile for the robust-python-demo project."""

import os
import re
import shlex
import shutil
from pathlib import Path
from textwrap import dedent
from typing import List
from typing import Pattern

import nox
from nox.command import CommandFailed
from nox.sessions import Session


nox.options.default_venv_backend = "uv"
os.environ.setdefault("PYO3_USE_ABI3_FORWARD_COMPATIBILITY", "1")

PYTHON_VERSIONS: List[str] = ["3.10", "3.11", "3.12", "3.13", "3.14"]
DEFAULT_PYTHON_VERSION: str = PYTHON_VERSIONS[-1]

REPO_ROOT: Path = Path(__file__).parent.resolve()
TESTS_FOLDER: Path = REPO_ROOT / "tests"
SCRIPTS_FOLDER: Path = REPO_ROOT / "scripts"
RUST_MANIFEST: Path = REPO_ROOT / "rust" / "Cargo.toml"
RUST_CORE_MANIFEST: Path = REPO_ROOT / "rust" / "core" / "Cargo.toml"
RUST_PYTHON_MANIFEST: Path = REPO_ROOT / "rust" / "python" / "Cargo.toml"
RUST_VERSION: str = "1.83"

RELEASE_BRANCH_RE: Pattern[str] = re.compile(r"^(release|hotfix)/")
DEFAULT_BASE: str = "origin/develop"
TAG_PREFIX: str = "v"
DIST_DIR: str = "dist"

PROJECT_NAME: str = "robust-python-demo"
PACKAGE_NAME: str = "robust_python_demo"
REPOSITORY_HOST: str = "github.com"
REPOSITORY_PATH: str = "robust-python/robust-python-demo"

ENV: str = "env"
FORMAT: str = "format"
LINT: str = "lint"
TYPE: str = "type"
TEST: str = "test"
COVERAGE: str = "coverage"
SECURITY: str = "security"
DOCS: str = "docs"
BUILD: str = "build"
RELEASE: str = "release"
QUALITY: str = "quality"


@nox.session(python=False, name="setup-venv", tags=[ENV])
def setup_venv(session: Session) -> None:
    """Set up the virtual environment for the current project."""
    session.run("python", SCRIPTS_FOLDER / "setup-venv.py", REPO_ROOT, "-p", PYTHON_VERSIONS[0], external=True)


@nox.session(python=False, name="setup-git", tags=[ENV])
def setup_git(session: Session) -> None:
    """Set up the git repo for the current project."""
    session.run("python", SCRIPTS_FOLDER / "setup-git.py", REPO_ROOT, external=True)


@nox.session(python=False, name="setup-remote")
def setup_remote(session: Session) -> None:
    """Set up the remote repository for the current project."""
    command: list[str | Path] = [
        "python",
        SCRIPTS_FOLDER / "setup-remote.py",
        REPO_ROOT,
        "--host",
        REPOSITORY_HOST,
        "--path",
        REPOSITORY_PATH,
    ]
    session.run(*command, external=True)


@nox.session(python=DEFAULT_PYTHON_VERSION, name="pre-commit", tags=[QUALITY])
def precommit(session: Session) -> None:
    """Lint using pre-commit."""
    args: list[str] = session.posargs or ["run", "--all-files", "--show-diff-on-failure"]

    session.log("Installing pre-commit dependencies...")
    session.install("-e", ".", "--group", "dev")

    session.run("pre-commit", *args)
    if args and args[0] == "install":
        activate_virtualenv_in_precommit_hooks(session)


@nox.session(python=False, name="format-python", tags=[FORMAT, QUALITY])
def format_python(session: Session) -> None:
    """Run Python code formatter (Ruff format)."""
    session.log(f"Running Ruff formatter check with py{session.python}.")
    session.run("uvx", "ruff", "format", *session.posargs)


@nox.session(python=False, name="lint-python", tags=[LINT, QUALITY])
def lint_python(session: Session) -> None:
    """Run Python code linters (Ruff check, Pydocstyle rules)."""
    session.log(f"Running Ruff check with py{session.python}.")
    session.run("uvx", "ruff", "check", "--fix", "--verbose")


@nox.session(python=PYTHON_VERSIONS, name="typecheck")
def typecheck(session: Session) -> None:
    """Run static type checking (Basedpyright) on Python code."""
    session.log("Installing type checking dependencies...")
    session.install("-e", ".", "--group", "dev")
    python_path: Path = Path(shutil.which("python", path=session.bin))

    session.log(f"Running Basedpyright check with py{session.python}.")
    session.run("basedpyright", "--pythonversion", session.python, "--pythonpath", python_path)


@nox.session(python=False, name="security-python", tags=[SECURITY])
def security_python(session: Session) -> None:
    """Run code security checks (Bandit) on Python code."""
    session.log(f"Running Bandit static security analysis with py{session.python}.")
    session.run("uvx", "bandit", "-r", PACKAGE_NAME, "-c", "bandit.yml", "-ll")

    session.log(f"Running pip-audit dependency security check with py{session.python}.")
    session.run("uvx", "pip-audit")


@nox.session(python=PYTHON_VERSIONS, name="tests-python", tags=[TEST])
def tests_python(session: Session) -> None:
    """Run the Python test suite (pytest with coverage)."""
    session.log("Installing test dependencies...")
    session.install("-e", ".", "--group", "dev")

    session.log(f"Running test suite with py{session.python}.")
    test_results_dir = TESTS_FOLDER / "results"
    test_results_dir.mkdir(parents=True, exist_ok=True)
    junitxml_file = test_results_dir / f"test-results-py{session.python.replace('.', '')}.xml"

    session.run(
        "pytest",
        "--cov={}".format(PACKAGE_NAME),
        "--cov-append",
        "--cov-report=term",
        "--cov-report=xml",
        f"--junitxml={junitxml_file}",
        "tests/",
    )


@nox.session
def doctor(session: nox.Session) -> None:
    """Verify local/CI parity preconditions (history, tags, tool versions)."""
    _ensure_full_history(session)
    _fetch_tags(session)
    session.run("uvx", "--from", "commitizen", "cz", "version")
    session.run("cargo", "--version", external=True)
    branch: str = _current_branch(session)
    last_tag: str = _git(session, "describe", "--tags", "--abbrev=0") or "<no tags yet>"
    session.log(f"branch={branch}  last_tag={last_tag}")
    dirty: bool = bool(_git(session, "status", "--porcelain"))
    session.log("working tree: " + ("DIRTY (fine for now, blocks release)" if dirty else "clean"))


@nox.session
def check_commits(session: nox.Session) -> None:
    """Lint commit messages (MR/PR gate).

    Usage: `nox -s check_commits`               -> origin/develop..HEAD
           `nox -s check_commits -- origin/main..HEAD`
    """
    rev_range: str = session.posargs[0] if session.posargs else f"{DEFAULT_BASE}..HEAD"
    session.run("uvx", "--from", "commitizen", "cz", "check", "--rev-range", rev_range)


@nox.session
def changelog_preview(session: nox.Session) -> None:
    """Preview the changelog section that the next bump would append."""
    _fetch_tags(session)
    session.run("uvx", "--from", "commitizen", "cz", "changelog", "--incremental", "--dry-run", *session.posargs)


@nox.session(python=DEFAULT_PYTHON_VERSION, name="build-docs", tags=[DOCS, BUILD])
def docs_build(session: Session) -> None:
    """Build the project documentation (Sphinx)."""
    session.log("Installing documentation dependencies...")
    session.install("-e", ".", "--group", "docs")

    session.log(f"Building documentation with py{session.python}.")
    docs_build_dir = Path("docs") / "_build" / "html"

    session.log(f"Cleaning build directory: {docs_build_dir}")
    session.run("sphinx-build", "-b", "html", "docs", str(docs_build_dir), "-E")

    session.log("Building documentation.")
    session.run("sphinx-build", "-b", "html", "docs", str(docs_build_dir), "-W")


@nox.session(python=DEFAULT_PYTHON_VERSION, name="docs", tags=[DOCS, BUILD])
def docs(session: Session) -> None:
    """Build and serve the project documentation (Sphinx)."""
    session.log("Installing documentation dependencies...")
    session.install("-e", ".", "--group", "docs")

    session.log(f"Building documentation with py{session.python}.")
    docs_build_dir = Path("docs") / "_build" / "html"

    session.log(f"Cleaning build directory: {docs_build_dir}")
    session.run("sphinx-build", "-b", "html", "docs", str(docs_build_dir), "-E")

    session.log("Building and serving documentation.")
    session.run("sphinx-autobuild", "--open-browser", "docs", str(docs_build_dir))


@nox.session(python=False, name="build-python", tags=[BUILD])
def build_python(session: Session) -> None:
    """Build sdist and wheel packages (uv build)."""
    session.log(f"Building sdist and wheel packages with py{session.python}.")
    session.run("uv", "build", "--sdist", "--wheel", "--out-dir", "dist/", external=True)
    session.log("Built packages in ./dist directory:")
    for path in Path("dist/").glob("*"):
        session.log(f"- {path.name}")


@nox.session(python=False, name="build-container", tags=[BUILD])
def build_container(session: Session) -> None:
    """Build the Docker container image.

    Requires Docker or Podman installed and running on the host.
    Ensures core project dependencies are synced in the current environment
    *before* the build context is prepared.
    """
    session.log("Building application container image...")
    try:
        session.run("docker", "info", success_codes=[0], external=True, silent=True)
        container_cli = "docker"
    except CommandFailed:
        try:
            session.run("podman", "info", success_codes=[0], external=True, silent=True)
            container_cli = "podman"
        except CommandFailed:
            session.log("Neither Docker nor Podman command found. Please install a container runtime.")
            session.skip("Container runtime not available.")

    current_dir: Path = Path.cwd()
    session.log(f"Ensuring core dependencies are synced in {current_dir.resolve()} for build context...")
    session.install("-e", ".")

    session.log(f"Building Docker image using {container_cli}.")
    project_image_name = PACKAGE_NAME.replace("_", "-").lower()
    session.run(
        container_cli,
        "build",
        str(current_dir),
        "-t",
        f"{project_image_name}:latest",
        "--progress=plain",
        external=True,
    )

    session.log(f"Container image {project_image_name}:latest built locally.")


def _run_release_tool(session: Session, command: str) -> None:
    """Delegate one provider-neutral release command to its local CLI."""
    base_command: list[str] = ["uv", "run", "--locked", "python", "-m", "scripts.release_tools"]
    session.run(*base_command, command, *session.posargs, external=True)


@nox.session(python=False, name="release-start")
def release_start(session: nox.Session) -> None:
    """Create release/X.Y.Z from the current branch (run this on develop)."""
    _ensure_full_history(session)
    _fetch_tags(session)
    _ensure_clean_tree(session)
    branch: str = _current_branch(session)
    if branch != "develop":
        session.warn(f"Cutting a release from '{branch}', not 'develop' — make sure that's intended.")
    version: str = _next_version(session)
    release_branch: str = f"release/{version}"
    session.run("git", "switch", "-c", release_branch, external=True)
    session.log(f"Created {release_branch}. Next: `nox -s release_preview`, then release_rc / release_final.")


@nox.session(python=False, name="release-preview")
def release_preview(session: nox.Session) -> None:
    """Dry-run the bump: shows increment, next version, and files touched."""
    _ensure_full_history(session)
    _fetch_tags(session)
    session.run("uvx", "--from", "commitizen", "cz", "bump", "--dry-run", *session.posargs)


@nox.session(python=False, name="release-rc")
def release_rc(session: nox.Session) -> None:
    """Tag a release candidate on the release branch (1.2.0-rc.1, -rc.2, ...)."""
    _release_preflight(session)
    push, forwarded = _split_local_flags(session.posargs)
    session.run("uvx", "--from", "commitizen", "cz", "bump", "--prerelease", "rc", *forwarded)
    if push:
        _push_with_tags(session)


@nox.session(python=False, name="release-final")
def release_final(session: nox.Session) -> None:
    """Final bump: writes Cargo.toml/Cargo.lock + CHANGELOG.md, commits, tags, pushes.

    Finalizing after RCs with no new commits? `nox -s release_final -- --allow-no-commit`.
    Forcing an increment: `nox -s release_final -- --increment PATCH`.
    """
    _release_preflight(session)
    push, forwarded = _split_local_flags(session.posargs)
    session.run("uvx", "--from", "commitizen", "cz", "bump", *forwarded)
    if push:
        _push_with_tags(session)
    session.log(
        "Done. Remember the gitflow tail: merge this branch --no-ff into master, "
        "then back-merge master into develop (mandatory with the cargo provider)."
    )


@nox.session(python=False, name="release-notes")
def release_notes(session: nox.Session) -> None:
    """Write one version's changelog section to release_notes.md.

    Usage: `nox -s release_notes -- 1.2.0` (or v1.2.0). Defaults to the latest tag.
    Feed the file to your forge's release-creation step.
    """
    _fetch_tags(session)
    if session.posargs:
        version: str = session.posargs[0]
    else:
        version: str = _git(session, "describe", "--tags", "--abbrev=0")
        if not version:
            session.error("No tags found and no version given.")
    version: str = version.removeprefix(TAG_PREFIX)
    notes: str | bool | None = session.run(
        "uvx", "--from", "commitizen", "cz", "changelog", version, "--dry-run", silent=True
    )
    if not isinstance(notes, str):
        raise ValueError(f"Failed to retrieve release notes from commitizen.")
    out_path: Path = Path("release_notes.md")
    out_path.write_text(notes, encoding="utf-8")
    session.log(f"Wrote {out_path} ({len(notes.splitlines())} lines) for version {version}.")


def _ensure_release_tag(session: nox.Session) -> str:
    """Publishing guard: HEAD must sit exactly on a final (non-rc) version tag."""
    tag: str = _git(session, "describe", "--tags", "--exact-match")
    if not tag:
        session.error("HEAD is not on a tag. Publishing is tag-triggered — check out the tag first.")
    if "-rc" in tag:
        session.error(f"{tag} is a release candidate; not publishing to immutable registries.")
    return tag


@nox.session(python=False, name="build-rust")
def build_rust(session: nox.Session) -> None:
    """Package and verify the crate (writes target/package/*.crate)."""
    session.run("cargo", "package", "--locked", *session.posargs, external=True)


@nox.session(python=False, name="build-python")
def build_python(session: nox.Session) -> None:
    """Builds the Python package into `./dist`.

    Builds for the current interpreter by default. To cover every supported
    CPython (3.10–3.14) installed on the machine, forward maturin's flag:
    `nox -s build_python -- --find-interpreter`. (If the crate uses PyO3's
    abi3-py310 feature, one wheel per platform already covers 3.10+ and
    --find-interpreter is unnecessary.)
    """
    session.run("maturin", "build", "--release", "--out", DIST_DIR, *session.posargs)
    session.run("maturin", "sdist", "--out", DIST_DIR)


@nox.session(python=False, name="build")
def build(session: nox.Session) -> None:
    """Umbrella: build the crate package, this platform's wheels, and the sdist."""
    session.notify("build-rust")
    session.notify("build-python")


@nox.session(python=False, name="publish-rust")
def publish_rust(session: nox.Session) -> None:
    """Publish the crate to crates.io. Requires CARGO_REGISTRY_TOKEN.

    Run on the tagged commit (guard enforces it); rc tags are refused.
    """
    _ensure_release_tag(session)
    session.run("cargo", "publish", "--locked", *session.posargs, external=True)


@nox.session(python=False, name="publish-python")
def publish_python(session: nox.Session) -> None:
    """Upload everything in ./dist (wheels + sdist) to PyPI.

    Artifact-passing model: build first — locally via build_wheels/build_sdist,
    or in CI by downloading the wheel-matrix artifacts into ./dist — then this
    session uploads the lot. Requires MATURIN_PYPI_TOKEN (or PyPI trusted
    publishing on a supported CI). --skip-existing makes retries idempotent.
    """
    _ensure_release_tag(session)
    artifacts: list[str] = sorted(str(path) for path in Path(DIST_DIR).glob("*") if path.is_file())
    if not artifacts:
        session.error(
            f"No artifacts in ./{DIST_DIR}. Run build_wheels/build_sdist first, "
            "or download the CI wheel-matrix artifacts into ./dist."
        )
    session.run("uvx", "maturin", "upload", "--skip-existing", *artifacts, *session.posargs)


@nox.session(python=False, name="publish")
def publish(session: nox.Session) -> None:
    """Publishes both the Python and Rust packages."""
    session.notify("publish-rust")
    session.notify("publish-python")


@nox.session(python=False)
def tox(session: Session) -> None:
    """Run the 'tox' test matrix.

    Requires uvx in PATH. Requires tox.ini file.
    Useful for specific ecosystem conventions (e.g., pytest plugins,
    cookiecutter-driven matrix testing).
    Accepts tox args after '--' (e.g., `nox -s tox -- -e py39`).
    """
    session.log("Running Tox test matrix via uvx...")
    session.install("-e", ".", "--group", "dev")

    tox_ini_path = Path("tox.ini")
    if not tox_ini_path.exists():
        session.log("tox.ini file not found at %s. Tox requires this file.", str(tox_ini_path))
        session.skip("tox.ini not present.")

    session.log("Checking Tox availability via uvx.")
    session.run("tox", "--version", success_codes=[0])

    session.run("tox", *session.posargs)


@nox.session(python=DEFAULT_PYTHON_VERSION, tags=[COVERAGE])
def coverage(session: Session) -> None:
    """Collect and report coverage.

    Requires tests to have been run with --cov and --cov-report=xml across matrix
    (e.g., via `nox -s test-python`).
    """
    session.log("Collecting and reporting coverage across all test runs.")
    session.log(
        "Note: Ensure 'nox -s test-python' was run across all desired Python versions first to generate coverage data."
    )

    session.log("Installing dependencies for coverage report session...")
    session.install("-e", ".", "--group", "dev")

    coverage_combined_file: Path = Path.cwd() / ".coverage"

    session.log("Combining coverage data.")
    try:
        session.run("coverage", "combine")
        session.log(f"Combined coverage data into {coverage_combined_file.resolve()}")
    except CommandFailed as e:
        if e.returncode == 1:
            session.log("No coverage data found to combine. Run tests first with coverage enabled.")
        else:
            session.error(f"Failed to combine coverage data: {e}")
        session.skip("Could not combine coverage data.")

    session.log("Generating HTML coverage report.")
    coverage_html_dir = Path("coverage-html")
    session.run("coverage", "html", "--directory", str(coverage_html_dir))

    session.log("Running terminal coverage report.")
    session.run("coverage", "report")

    session.log(f"Coverage reports generated in ./{coverage_html_dir} and terminal.")


def _git(session: nox.Session, *args: str) -> str:
    """Run a git command and return its stdout (stripped)."""
    out: str | bool | None = session.run("git", *args, external=True, silent=True)
    if isinstance(out, str):
        return out.strip()
    return ""


def _current_branch(session: nox.Session) -> str:
    branch: str = _git(session, "rev-parse", "--abbrev-ref", "HEAD")
    if not branch:
        raise RuntimeError(f"Failed to get current branch.")
    return branch


def _ensure_clean_tree(session: nox.Session) -> None:
    if _git(session, "status", "--porcelain"):
        session.error(
            "Working tree is not clean. `cz bump` commits and tags whatever it "
            "finds — commit or stash your changes first."
        )


def _ensure_full_history(session: nox.Session) -> None:
    if _git(session, "rev-parse", "--is-shallow-repository") == "true":
        session.error(
            "Shallow clone detected. Commitizen computes the increment and "
            "changelog from history since the last tag; a shallow clone gives "
            "DIFFERENT results than a full one. Locally: `git fetch --unshallow "
            "--tags`. GitHub: fetch-depth: 0. GitLab: GIT_DEPTH: 0. "
            "Bitbucket: clone depth 'full'."
        )


def _fetch_tags(session: nox.Session) -> None:
    """Best-effort tag sync so local and CI see the same 'latest version'."""
    try:
        session.run("git", "fetch", "--tags", "--quiet", external=True)
    except Exception:
        session.warn("Could not fetch tags (offline?) — proceeding with local tags.")


def _ensure_release_branch(session: nox.Session) -> str:
    branch: str = _current_branch(session)
    if not RELEASE_BRANCH_RE.match(branch):
        session.error(
            f"Refusing to bump on '{branch}'. Releases happen on release/* or "
            "hotfix/* branches (gitflow Variant A). Use `nox -s release_start` "
            "first, or check out the branch you meant."
        )
    return branch


def _release_preflight(session: nox.Session) -> None:
    _ensure_full_history(session)
    _fetch_tags(session)
    _ensure_clean_tree(session)
    _ensure_release_branch(session)


def _split_local_flags(posargs: list[str]) -> tuple[bool, list[str]]:
    """Extract flags this noxfile owns (--no-push); forward the rest to cz."""
    push: bool = "--no-push" not in posargs
    return push, [arg for arg in posargs if arg != "--no-push"]


def _push_with_tags(session: nox.Session) -> None:
    session.run("git", "push", "--follow-tags", external=True)


def _next_version(session: nox.Session) -> str:
    """Ask commitizen for the next version without changing anything.

    Prefers `cz bump --get-next`; falls back to parsing `--dry-run` output for
    older commitizen versions or configs where --get-next is restricted.
    """
    try:
        out: str | bool | None = session.run(
            "uvx", "--from", "commitizen", "cz", "bump", "--get-next", silent=True
        )
        version = (out or "").strip().splitlines()[-1].strip()
        if version:
            return version
    except Exception:
        pass


def _next_version_fallback(session: nox.Session) -> str:
    """Gets the next version using the --dry-run fallback from commitizen."""
    out: str | bool | None = session.run(
        "uvx", "--from", "commitizen", "cz", "bump", "--dry-run", silent=True, success_codes=[0]
    )
    if not isinstance(out, str):
        raise RuntimeError(f"Failed to get next version from commitizen.")
    match: re.Match | None = re.search(r"tag to create:\s*\S*?(\d[\w.\-+]*)\s*$", out, re.MULTILINE)
    if not match:
        session.error(
            "Could not determine the next version. Are there any bumpable "
            "(feat/fix/BREAKING) commits since the last tag?"
        )
    return match.group(1)


def activate_virtualenv_in_precommit_hooks(session: Session) -> None:
    """Activate virtualenv in hooks installed by pre-commit.

    This function patches git hooks installed by pre-commit to activate the
    session's virtual environment. This allows pre-commit to locate hooks in
    that environment when invoked from git.

    Args:
        session: The Session object.
    """
    assert session.bin is not None  # nosec

    # Only patch hooks containing a reference to this session's bindir. Support
    # quoting rules for Python and bash, but strip the outermost quotes so we
    # can detect paths within the bindir, like <bindir>/python.
    bindirs = [
        bindir[1:-1] if bindir[0] in "'\"" else bindir for bindir in (repr(session.bin), shlex.quote(session.bin))
    ]

    virtualenv = session.env.get("VIRTUAL_ENV")
    if virtualenv is None:
        return

    headers = {
        # pre-commit < 2.16.0
        "python": f"""\
            import os
            os.environ["VIRTUAL_ENV"] = {virtualenv!r}
            os.environ["PATH"] = os.pathsep.join((
                {session.bin!r},
                os.environ.get("PATH", ""),
            ))
            """,
        # pre-commit >= 2.16.0
        "bash": f"""\
            VIRTUAL_ENV={shlex.quote(virtualenv)}
            PATH={shlex.quote(session.bin)}"{os.pathsep}$PATH"
            """,
        # pre-commit >= 2.17.0 on Windows forces sh shebang
        "/bin/sh": f"""\
            VIRTUAL_ENV={shlex.quote(virtualenv)}
            PATH={shlex.quote(session.bin)}"{os.pathsep}$PATH"
            """,
    }

    hookdir: Path = Path(".git") / "hooks"
    if not hookdir.is_dir():
        return

    for hook in hookdir.iterdir():
        if hook.name.endswith(".sample") or not hook.is_file():
            continue

        if not hook.read_bytes().startswith(b"#!"):
            continue

        text: str = hook.read_text()

        if not any((Path("A") == Path("a") and bindir.lower() in text.lower()) or bindir in text for bindir in bindirs):
            continue

        lines: list[str] = text.splitlines()

        for executable, header in headers.items():
            if executable in lines[0].lower():
                lines.insert(1, dedent(header))
                hook.write_text("\n".join(lines))
                break
