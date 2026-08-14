# Releasing

This project uses one release process locally and in GitHub Actions. A release pull request validates code that has no registry credentials. Publication starts only after GitHub merges that pull request into `main`.

## Repository setup

Complete this setup before the first release.

1. Protect the `main` and `develop` branches.
   - Require pull requests and required checks.
   - Prevent force pushes and deletion.
   - Permit the release workflow to create `backmerge/*` branches.
2. Add a `v*` tag ruleset.
   - Prevent update and deletion of release tags.
   - Permit tag creation only for designated maintainers and `.github/workflows/finalize-release.yml`.
3. Create these GitHub environments:

   - `testpypi`
   - `release-tag`
   - `pypi`

4. Add required reviewers and deployment-branch restrictions to each environment.
5. Configure trusted publishers.
   - On TestPyPI and PyPI, select this GitHub repository, `finalize-release.yml`, and the matching environment name.

The workflows use GitHub OpenID Connect tokens. Do not add long-lived PyPI or crates.io tokens.

## Prepare a release

Start from a clean `develop` branch.

```console
git switch develop
git pull --ff-only
uv run --locked nox -s prepare-release -- <increment-or-version>
```

The command creates `release/<version>`, synchronizes the Python and Cargo metadata, updates `CHANGELOG.md`, validates the result, and creates the release preparation commit. Push the branch and open a pull request into `main`.

The **Release Check** workflow performs the same validation. It also builds and smoke-tests every release artifact. It cannot tag or publish.

## Finalize a release

Merge the release pull request after its required checks pass. The **Finalize Release** workflow then performs these operations in order:

1. Validate the exact merge commit.
2. Build the complete artifact matrix once.
3. Publish the files to TestPyPI and verify their names, metadata, and hashes.
4. Create annotated tag `v<version>` at the validated commit.
5. Publish the configured crates.io package.
6. Publish the same Python files to PyPI.
7. Create the GitHub release from the committed changelog section.
8. Open a `main` to `develop` backmerge pull request.

TestPyPI is a required gate. A TestPyPI failure stops tag creation and every production publication.

## Recovery

Rerun the failed job after you correct an environment or service problem. Each registry check permits an existing file only when its filename, version, metadata, and hash match the release manifest. If an immutable registry file conflicts, stop and prepare a new version.

Use the **Finalize Release** manual dispatch when the automatic run cannot resume. Supply the version without `v` and the full 40-character commit ID. A maintainer can also push an annotated `v*` tag at the validated commit to start the recovery path. Both routes repeat validation and TestPyPI verification.

Use these failure-specific rules:

- **Build or validation failure:** Correct the release branch and update the release pull request. Do not publish manually.
- **TestPyPI failure before tag creation:** Correct the environment or trusted publisher, then rerun. If TestPyPI contains a conflicting file, use a new version.
- **Tag failure:** Confirm the `release-tag` environment and tag ruleset. Never move or replace an existing release tag.
- **crates.io or PyPI failure:** Keep the tag. Correct the publisher configuration and rerun from the same commit. Do not rebuild artifacts.
- **GitHub release failure:** Rerun after all configured registries contain matching artifacts. The workflow reuses the committed changelog and release bundle.
- **Backmerge conflict:** Merge `main` into a branch based on `develop`, resolve the conflict locally, and open the backmerge pull request manually.
- **Automation pull request waits for checks:** Approve the GitHub Actions run for the backmerge pull request. The workflow intentionally uses `GITHUB_TOKEN` instead of a personal token or GitHub App.

For local tag recovery, validate the exact target before you push it:

```console
uv run --locked nox -s finalize-release -- <version> <full-commit-id>
git push origin refs/tags/v<version>
```

Do not use local registry publication as a recovery shortcut. The protected workflow owns TestPyPI, PyPI, crates.io, and GitHub release publication.
