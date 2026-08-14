# Rust workspace naming, Python ABI, and publication

- Status: accepted
- Date: 2026-08-14
- Decision makers: project maintainers
- Consulted: contributors
- Informed: Rust and Python package users

## Context and problem statement

This project does not currently include a Rust extension. The template still records the Rust structure used if maintainers regenerate or migrate the project with Rust support.

## Decision drivers

- Match Python's external-name and import-name distinction.
- Keep Cargo, crates.io, Rust import, and PyO3 module names unambiguous.
- Keep reusable Rust logic separate from the Python binding boundary.
- Support `abi3` and per-version CPython wheels explicitly.
- Make crates.io publication opt-in.
- Keep one version across Python and Rust release metadata.

## Considered options

- A Cargo workspace with separate core and Python binding crates
- One PyO3 crate containing all Rust code
- Independent Rust and Python repositories and versions

## Decision outcome

Chosen option: **A Cargo workspace with separate core and Python binding crates**.

`rust_project_name` is the external, hyphenated Cargo and crates.io package name. `rust_package_name` is its underscore-form Rust crate identifier. The two values must normalize to the same name. The private binding Cargo package uses the normalized Python project name with a `-python` suffix. Its library and PyO3 extension module use `robust_python_demo`.

The core and binding crates inherit the workspace version. `pyproject.toml` remains release-version authority. The core crate is not publishable unless `publish_rust_package` is enabled. The selected `rust_python_abi` determines whether CI builds one compatible wheel per platform (`abi3`) or one wheel per configured Python version (`cpython`).

### Consequences

- Good, because Cargo users see the expected hyphenated package name.
- Good, because Rust source uses the expected underscore identifier.
- Good, because the Python extension keeps the existing Python import name.
- Good, because pure Rust logic can be tested and published without the binding layer.
- Bad, because workspace metadata and lockfiles require synchronized validation.
- Bad, because CPython ABI mode increases the release matrix substantially.
- Neutral, because enabling crates.io publication later is supported, while disabling it after public releases cannot remove published versions.

### Confirmation

Generation and release checks must confirm that:

- the core `[package].name` is `rust_project_name`;
- the core `[lib].name` is `rust_package_name`;
- `rust_package_name` is the underscore normalization of `rust_project_name`;
- the binding module is `robust_python_demo`;
- both crates use the workspace version; and
- Cargo packaging and publish dry-run checks succeed when Rust publication is enabled.

## Pros and cons of the options

### A Cargo workspace with separate core and Python binding crates

- Good, because it represents the two publication boundaries directly.
- Good, because the core can remain private without changing the Python package.
- Bad, because workspace configuration is more detailed than a single crate.

### One PyO3 crate containing all Rust code

- Good, because it has fewer manifests.
- Bad, because reusable Rust code and Python binding details share one public boundary.
- Bad, because optional crates.io publication becomes unclear.

### Independent Rust and Python repositories and versions

- Good, because each ecosystem can release independently.
- Bad, because the extension must coordinate two repositories and version lines.
- Bad, because it does not match this repository's single-project release model.

## More information

See [Releasing](../releasing.md) for build matrices and publication setup.
