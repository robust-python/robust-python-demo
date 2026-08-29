# Provider-neutral tag releases

- Status: accepted
- Date: 2026-08-28
- Decision makers: project maintainers
- Consulted: contributors
- Informed: package users
- Supersedes: [ADR 0001](0001-release-publication-authority.md)

## Context and problem statement

The previous release process treated GitHub Actions as the release authority. It used a custom Python program to coordinate version parsing, TestPyPI, registry reconciliation, tag creation, GitHub Releases, and backmerge pull requests. That process was difficult to maintain and could not provide the same release contract on GitHub, GitLab, and Bitbucket.

The project needs one release model that maintainers can run locally. CI providers must call the same release commands. Provider-specific files can supply runners, artifact transfer, workload identity, protected environments, and native release pages. They must not own version or changelog decisions.

## Decision drivers

- Keep version calculation and changelog generation in Commitizen.
- Keep validation and artifact construction locally reproducible through Nox.
- Use one release trigger on each supported Git provider.
- Keep registry credentials out of pull-request and merge-request pipelines.
- Use trusted publishing when a registry accepts the CI provider's identity.
- Do not add long-lived credentials to emulate trusted publishing on an unsupported provider.
- Prefer standard tool failures and documented recovery to a custom cross-registry state machine.

## Considered options

- Keep GitHub finalization and add partial GitLab and Bitbucket copies.
- Build a provider-neutral custom release application with forge adapters.
- Use Commitizen, Nox, and standard publishing tools with thin CI adapters.
- Publish only from a maintainer workstation.

## Decision outcome

Chosen option: **Use Commitizen, Nox, and standard publishing tools with thin CI adapters**.

Commitizen owns the release version and generated changelog. It uses SemVer 2 source syntax so stable and prerelease versions can be written to Python and Cargo metadata without a custom translator. The `uv` version provider updates Python metadata and `uv.lock`; Maturin projects also list the Cargo workspace version as a Commitizen version file.

Nox exposes the local release tasks but does not implement release policy. Each release session invokes the locked environment and delegates once to the Typer interface in `scripts.release_tools`. The release tool validates command input and external-tool output, then invokes Commitizen, Git, uv, Maturin, Cargo, and Twine directly through one subprocess boundary. Direct subprocess execution keeps the operations callable without a Nox session and gives them one typed error contract. It also means the release tool, rather than Nox, owns command output and failure translation.

The release-tool modules separate metadata, Git state, artifact construction, consumer smoke tests, Rust compatibility, and publication. The focused adapter is a substantial maintenance surface: currently about 900 production lines across 14 modules. Only `scripts/release_tools` is included in the generated project's static type-checking scope; unrelated maintenance scripts retain their existing validation. The release tool must not calculate versions, reconcile registries, call forge APIs, create remote state, or manage backmerges.

The complete release-tool package is rendered for pure-Python projects and for projects whose Rust core is private. Rust publication commands are not registered in those configurations, and the publication operations enforce the same policy for direct callers. Retaining dormant Rust code and configuration keeps one type-checked implementation and makes later Cruft transitions less disruptive. The cost is deliberate dead code in projects that do not publish a Rust crate.

Maintainers prepare `release/<version>` from `develop` and open a pull request or merge request into `main`. Review pipelines validate metadata and build release artifacts without publication credentials. After merge, a maintainer creates an annotated `v<version>` tag at the merged `main` commit. The tag is the only production release trigger.

GitHub Actions publishes with workload identity when PyPI accept the configured trusted publisher. GitLab publishes Python artifacts with workload identity only after a maintainer enables the complete runner matrix. Bitbucket validates and builds the release, but it does not publish by default because the registries do not accept Bitbucket Pipelines as a trusted publisher. A maintainer publishes a Bitbucket-hosted project locally unless the project explicitly accepts the cost of protected registry tokens.

Commitizen-generated notes are the native GitHub or GitLab Release description. The project does not maintain a second parser for reviewed changelog sections. Bitbucket uses the annotated tag and committed changelog because Bitbucket Cloud has no equivalent immutable Release object.

TestPyPI is an optional manual rehearsal. It is not a production gate. Maintainers use a real prerelease when they need to test installation through the production index.

Publication across Git, PyPI, and a forge Release is not atomic. The workflow creates the forge Release last. If an earlier publication succeeds and a later publication fails, a maintainer completes recovery from the same tag.

After publication, a maintainer opens a `main` to `develop` backmerge pull request or merge request. The next release must not start until the release tag is in `develop`'s ancestry.

### Consequences

- Good, because local and hosted release jobs call the same Nox and Commitizen interfaces.
- Good, because Nox remains a task catalog instead of becoming the release application.
- Good, because the same tag trigger works on GitHub, GitLab, and Bitbucket.
- Good, because the project removes custom version, registry, release, and backmerge state machines.
- Good, because pull-request and merge-request jobs do not receive publication credentials.
- Good, because provider limitations are explicit instead of hidden behind incomplete equivalence.
- Bad, because a maintainer must create the final tag and backmerge request.
- Bad, because Bitbucket cannot publish without a maintainer or long-lived registry tokens.
- Bad, because GitLab and Bitbucket need suitable native runners before they can publish a complete Maturin wheel matrix.
- Bad, because provider-neutral Linux portability requires the large locked Zig tool in each Maturin development environment.
- Bad, because Commitizen-generated release notes cannot include unrepresented manual edits to the committed changelog.
- Bad, because recovery is a documented maintainer operation instead of an automatic registry reconciliation process.
- Bad, because the project must maintain a focused but substantial release-tool adapter around the standard commands.
- Bad, because consumer smoke tests resolve and install runtime dependencies. This checks the real consumer installation contract, but requires index/network availability when dependencies are not cached.
- Bad, because direct subprocess execution does not reuse Nox's command-running abstraction; the release tool must maintain its own narrow process and error boundary.
- Bad, because a tag or one registry publication can remain after a later publication failure.
- Bad, because the simplified flow no longer creates or reconciles an artifact hash manifest.
- Bad, because registry publication and forge artifacts still have no signed provenance or SBOM attestation.
- Neutral, because TestPyPI remains available as an explicit rehearsal but no longer delays every release.
- Neutral, because provider-native Release pages are presentation layers and not release authority.

### Confirmation

The generated project must show that:

- Commitizen updates the version and changelog before review;
- each Nox release session delegates to the locally callable release tool without interpreting release state;
- the generated type checker reports no warnings or errors for `scripts/release_tools`;
- consumer smoke tests install dependencies and import only the configured Python package;
- release review pipelines have no registry credentials;
- production publication starts only from an annotated `v*` tag;
- the tag version, Commitizen version, and built artifact version agree;
- hosted publication jobs use protected workload identity and exist only for supported provider/registry combinations;
- Bitbucket does not contain registry tokens or production upload commands by default;
- native publication requires every configured platform artifact;
- the forge Release depends on all configured production publications; and
- the release procedure requires a completed `main` to `develop` backmerge.

## Pros and cons of the options

### Keep GitHub finalization and add partial provider copies

- Good, because GitHub retains the existing automatic recovery behavior.
- Bad, because GitHub remains the real release authority.
- Bad, because the provider copies drift as the GitHub workflow changes.

### Build a provider-neutral custom release application

- Good, because one application can normalize provider APIs.
- Bad, because the project must maintain authentication, registry, artifact, and forge clients.
- Bad, because the application would duplicate standard tool behavior.

### Use Commitizen, Nox, and standard publishing tools

- Good, because each tool owns the contract it already implements.
- Good, because maintainers can reproduce the important operations locally.
- Bad, because provider authentication and runner availability still differ.

### Publish only from a maintainer workstation

- Good, because it works with every Git provider.
- Bad, because workstation credentials and state become the normal publication authority.
- Bad, because native artifact construction is difficult to reproduce across platforms.

## More information

See [Releasing](../releasing.md) for provider setup, release steps, and recovery.
