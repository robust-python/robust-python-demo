# Release pipeline sacrifice audit

> Historical note: [ADR 0003](decisions/0003-provider-neutral-tag-releases.md) supersedes the release process reviewed here. The current process uses Commitizen, Nox, and a maintainer-created tag. It no longer provides the TestPyPI gate, automatic registry reconciliation, GitHub-only finalization, or automatic backmerge described in this audit.

This audit covers the template changes introduced in the range
`37cfec5..fc7237f` and their rendered Python and Maturin demos. It records
costs and regressions that were not clear enough when the release design was
approved.

## Intentional and previously disclosed

- GitHub Actions is the only complete publication orchestrator. GitLab and
  Bitbucket projects keep the local validation and build commands, but the
  template no longer generates registry-writing jobs for those providers.
- TestPyPI is a mandatory gate before the production tag and production
  registries.
- A Maturin project uses a Cargo workspace with a private Python binding crate
  and an optionally published core crate. Python and Rust share one release
  version.
- Publication uses trusted publishing. The workflow does not use a personal
  access token or GitHub App.

## Intentional but insufficiently disclosed

- `abi3` reduces the wheel matrix by limiting the extension to Python's stable,
  limited C API. An extension that needs APIs outside that subset must use
  `cpython`, which builds a platform-by-Python-version matrix.
- The binding crate keeps Cargo's Rust test harness enabled. Those tests link to
  Python, so the Rust test session selects its uv-managed interpreter explicitly
  and the runner must provide compatible Python development and runtime shared
  libraries. Windows also requires the selected interpreter's base prefix on
  `PATH`; otherwise an `abi3` test executable can load an unrelated `python3.dll`
  or a CPython test executable can fail to find its versioned DLL. The session
  does not set `PYTHONHOME` or Unix loader variables without evidence of a
  platform-specific failure. Core tests remain independent of Python. The
  binding remains a `cdylib`; an `rlib` is needed only for external Rust
  integration tests or downstream Rust users.
- Raw Cargo build automation intentionally covers only the reusable core crate.
  Maturin owns private binding artifacts because it supplies the Python
  extension build configuration. The tradeoff is that the binding has no
  generic raw-Cargo cross-target build coverage beyond native Cargo tests;
  cross-platform artifact coverage comes from the Maturin wheel matrix and its
  smoke tests.
- The registry round trip installs only a wheel compatible with its verification
  runner. Every other downloaded file is checked against its expected filename,
  metadata, size, and hash. Each native wheel is installed and smoke-tested on
  its build runner before upload.
- "Built once" means once within one workflow run. The release bundle is retained
  for 30 days. After it expires, recovery requires a rebuild whose files must
  match any immutable files already in a registry. Nondeterministic artifact
  bytes can make that recovery impossible for the existing version.
- Publication cannot be atomic across independent services. The production tag
  and Rust crate can exist before a later PyPI failure. The GitHub Release stays
  absent until every configured production registry succeeds.
- The initial implementation introduced a 959-line custom release tool, and the
  repaired interfaces add more boundary validation. The tool parses a deliberately
  narrow subset of versions, TOML assignments, lockfiles, wheel names, source
  distributions, and registry responses. That code has an ongoing test and
  maintenance cost. This repair isolates the parsers but does not add another
  parsing dependency; replacing them requires a separate cost-benefit decision.
- Twine and Maturin are direct development dependencies. Action version tags
  require Dependabot maintenance and can move when their upstream maintainers
  retarget a tag. The native matrix consumes CI minutes and artifact storage,
  and macOS and preview ARM runners can materially increase private-repository
  cost.
- Protected environments require repository setup and may not provide required
  reviewers for private repositories on every GitHub plan. Maintainers must
  confirm the features available to their organization before relying on human
  approval as a control.
- The hash manifest checks identity and integrity. It is not signed provenance,
  an SBOM, or an artifact attestation. Those controls remain outside this change.

## Amplified inherited problems

- The previous release helper already attempted automatic rollback. The new
  implementation amplified the risk by staging every path with `git add --all`
  and using `reset --hard`, `clean -fd`, checkout, and branch deletion. That
  could capture unrelated work in the release commit or destroy concurrent work.
  The repaired command stages only release-owned files and preserves the release
  branch and generated evidence on failure.
- Existing workflows used shell commands, but the release implementation moved
  release identity, registry-state decisions, GitHub Release reconciliation,
  and backmerge behavior into GitHub-only multiline Bash. The repaired workflow
  keeps only runner wiring and direct tool invocations in YAML. Business rules
  are named, locally callable Nox interfaces with tests.

## Implementation defects

- The backmerge job used a predictable branch and `--force-with-lease`. A rerun
  could replace work already on that branch. The repaired command never force
  pushes and accepts an existing branch only when its open pull request and
  two-parent merge identity exactly match the released commit.
- The setup guide claimed a tag ruleset could authorize one workflow file.
  GitHub rulesets grant bypass to actors or apps, not workflow-file identities.
  The corrected design protects `v*` tags from update and deletion, gates the
  normal tag job with the `release-tag` environment, and keeps maintainer-created
  annotated tags as the recovery path.
- The setup guide omitted the repository setting that lets `GITHUB_TOKEN` create
  pull requests. Workflows on that automation-created pull request require
  maintainer approval, as already accepted in the design.
- Some runner labels were obsolete or unstable, and action reference conventions
  differed between workflows. Generated GitHub workflows now use supported runner
  labels and readable version references consistently.
- The crates.io probe treated any failed request as "not published." It now
  distinguishes `404` from transport, API, and schema failures, and distinguishes
  an identical checksum from an immutable conflict.
- GitHub Release reruns used editing and `--clobber`. They now create only an
  absent release or accept an exact match of tag target, title, note bytes, and
  the complete asset set. Any other existing state fails without mutation.
- Recovery documentation simultaneously prohibited rebuilding and depended on
  expiring artifacts. It now states the actual retention and rebuild boundary.

## Residual boundaries

The repair preserves GitHub-first publication and does not restore GitLab or
Bitbucket registry writes. It also does not add a PAT, GitHub App, parsing
dependency, provenance attestation, or SBOM generator. Each would introduce a
separate authority, dependency, or operating-cost decision.

See [Releasing](releasing.md) for the resulting setup and recovery procedure.
