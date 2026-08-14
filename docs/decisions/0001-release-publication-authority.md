# Release publication authority

- Status: accepted
- Date: 2026-08-14
- Decision makers: project maintainers
- Consulted: contributors
- Informed: package users

## Context and problem statement

A release changes several immutable systems: Git tags, TestPyPI, PyPI, and GitHub Releases. Local and CI release paths must agree on the version and changelog. Credentials must not be available to pull-request code. Partial failures must not cause a rebuild or a conflicting upload.

## Decision drivers

- Reproduce release validation and preparation locally.
- Publish only artifacts built from the validated `main` commit.
- Test the exact Python artifacts on TestPyPI before production publication.
- Keep registry credentials out of pull-request jobs.
- Make reruns safe for immutable registry versions.
- Use GitHub's short-lived identity tokens and least-privilege job permissions.
- Preserve an explicit recovery path without creating a second release process.

## Considered options

- Protected GitHub finalization after a locally prepared release pull request
- Local publication by a maintainer
- Publication from every release-branch push
- Tag-first publication triggered only by a pushed tag

## Decision outcome

Chosen option: **Protected GitHub finalization after a locally prepared release pull request**.

Maintainers prepare `release/<version>` from `develop` with Nox. Pull-request CI validates and builds without write credentials. After merge, GitHub Actions builds the exact merge commit once, publishes and verifies it on TestPyPI, creates an annotated tag, publishes to the configured production registries, creates the GitHub release, and opens a backmerge pull request.

The workflow records artifact hashes before publication. A rerun skips an existing registry file only when it matches that manifest. A conflict requires a new version. Manual dispatch and a maintainer-pushed annotated tag are recovery triggers, not alternate publication authorities.

### Consequences

- Good, because pull-request code cannot access registry identities.
- Good, because TestPyPI verifies the production artifact set before the production tag exists.
- Good, because PyPI receives the files that passed TestPyPI verification.
- Good, because protected environments can require approval at each irreversible boundary.
- Good, because the changelog committed with the release is the GitHub release-note source.
- Bad, because a release uses several sequential jobs and can take longer than direct publication.
- Bad, because TestPyPI filename conflicts require a new release version.
- Neutral, because GitHub is the first release orchestrator while Nox remains the vendor-neutral local interface.

### Confirmation

The release workflows must show that:

- `release/*` pull requests have read-only permissions;
- TestPyPI succeeds before tag creation;
- each publishing job has only its required `id-token` or `contents` permission;
- production jobs download the recorded release bundle instead of rebuilding it; and
- the GitHub release job depends on every configured production publication.

## Pros and cons of the options

### Protected GitHub finalization after a locally prepared release pull request

- Good, because it separates reviewable preparation from irreversible publication.
- Good, because protected environments establish clear authority.
- Bad, because recovery depends on retained workflow artifacts or an identical rebuild that passes manifest checks.

### Local publication by a maintainer

- Good, because a maintainer can act without CI availability.
- Bad, because local credentials and workstation state become release authority.
- Bad, because it is harder to prove that all registries received the same files.

### Publication from every release-branch push

- Good, because TestPyPI feedback arrives before merge.
- Bad, because untrusted pull-request code reaches a publishing identity.
- Bad, because repeated branch updates consume immutable TestPyPI versions.

### Tag-first publication triggered only by a pushed tag

- Good, because a tag provides a simple trigger.
- Bad, because the production tag exists before TestPyPI verification.
- Bad, because a failed staging check leaves a tag that appears releasable.

## More information

See [Releasing](../releasing.md) for repository configuration and recovery procedures.
