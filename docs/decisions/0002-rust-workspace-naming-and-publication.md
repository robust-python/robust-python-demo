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

`rust_project_name` is the external, hyphenated Cargo and crates.io package name. `rust_package_name` is its underscore-form Rust crate identifier. The two values must normalize to the same name. The private binding Cargo package uses the normalized Python project name with a `-python` suffix. Its library and PyO3 extension module use the private name `_native`; the Python package re-exports the supported API from that implementation module.

The core and binding crates inherit the workspace version. `pyproject.toml` remains release-version authority. The core crate is not publishable unless `publish_rust_package` is enabled. The selected `rust_python_abi` determines whether CI builds one compatible wheel per platform (`abi3`) or one wheel per configured Python version (`cpython`).

Linux release wheels use Maturin's Zig integration with the `manylinux2014` policy. The release build interface applies this policy automatically for Linux targets so local, GitHub, GitLab, and Bitbucket builds cannot silently produce host-specific `linux` wheels. Native smoke tests still run on the target architecture.

When core publication is enabled, release validation uses `cargo-semver-checks`
against the latest stable, non-yanked crates.io release. The first publication
and a prerelease-only history have no stable baseline and skip this comparison.
Registry transport and schema failures remain errors. The checker is installed
from its current locked crate release because its rustdoc JSON support must move
with stable Rust; this adds network, installation, and CI-time costs and means
the checker version is not fixed by this project's `Cargo.lock`.

The workspace declares `rust-version = "1.83"` as its minimum supported Rust version (MSRV). Normal Cargo and Maturin automation uses the committed lockfile, and a separate local and CI session tests the complete workspace with that compiler version.

The binding crate keeps Cargo's test harness enabled. It does not enable PyO3's `extension-module` feature permanently. Maturin 1.9.4 or newer sets `PYO3_BUILD_EXTENSION_MODULE` only while packaging the extension, so ordinary Cargo tests can link to Python. The test-only PyO3 dependency uses `auto-initialize`, and the Rust test session selects its own Python interpreter explicitly.

Generic raw-Cargo build automation intentionally targets only the reusable core crate. Maturin owns private binding artifacts because it applies the Python extension build configuration and produces the platform-specific Python packages. The binding still runs native Cargo tests, and the release wheel matrix builds and smoke-tests its distributable artifacts.

### Consequences

- Good, because Cargo users see the expected hyphenated package name.
- Good, because Rust source uses the expected underscore identifier.
- Good, because the Python extension keeps the existing Python import name.
- Good, because `_native` clearly separates the supported Python facade from the implementation module and avoids IDE ambiguity between the package and extension.
- Good, because `_native.pyi` and `py.typed` expose the native boundary to static type checkers.
- Good, because pure Rust logic can be tested and published without the binding layer.
- Good, because source builders and Rust consumers receive an explicit compiler compatibility contract.
- Good, because the binding crate keeps its Cargo test harness and can test PyO3 registration and calls directly.
- Good, because raw Cargo cross-target builds exercise the independently reusable and optionally publishable Rust core without accidentally treating the private binding as a standalone Rust artifact.
- Bad, because workspace metadata and lockfiles require synchronized validation.
- Bad, because the native stub must remain synchronized with exported PyO3 functions.
- Bad, because the MSRV job adds CI time and can constrain dependency upgrades until maintainers deliberately raise the contract.
- Bad, because CPython ABI mode increases the release matrix substantially.
- Bad, because `abi3` restricts extension code to Python's limited API even though it reduces the matrix to one wheel per platform.
- Bad, because binding-crate Cargo tests link to Python and therefore require a compatible interpreter, development library, and runtime shared library for the Rust toolchain.
- Bad, because the Rust test session must select its uv-managed interpreter explicitly rather than relying on whichever Python appears first on `PATH`. On Windows it must also prepend that interpreter's base prefix to `PATH` so the test executable loads the matching Python DLL.
- Bad, because the private binding has no generic raw-Cargo cross-target build matrix beyond its native Cargo tests; cross-platform binding coverage instead comes from the Maturin wheel matrix.
- Bad, because the CPython matrix increases CI time, storage, and macOS and Linux ARM runner cost.
- Bad, because the locked Zig tool adds a large download, cache, and installation-time cost to Maturin development environments.
- Bad, because `manylinux2014` establishes a glibc 2.17 floor and can reject future native dependencies that require newer symbols or cannot build through Zig.
- Neutral, because the template does not produce `musllinux` wheels.
- Neutral, because each native artifact is smoke-tested on its compatible build runner, but the simplified provider-neutral flow does not add signed provenance, an SBOM, or registry round-trip hash verification.
- Neutral, because enabling crates.io publication later is supported, while disabling it after public releases cannot remove published versions.
- Neutral, because the private binding remains a `cdylib`; projects need an additional `rlib` crate type only when external Rust integration tests or downstream Rust code must import it.
- Neutral, because the test session does not set `PYTHONHOME` or Unix loader variables. Those variables can change interpreter or loader behavior globally and will be added only if a native runner demonstrates a separate resolution failure.

### Confirmation

Generation and release checks must confirm that:

- the core `[package].name` is `rust_project_name`;
- the core `[lib].name` is `rust_package_name`;
- `rust_package_name` is the underscore normalization of `rust_project_name`;
- the binding module is `robust_python_demo._native` and its public exports have matching stubs;
- both crates use the workspace version; and
- both crates inherit the declared workspace MSRV;
- core Cargo tests run without Python while binding Cargo tests use the selected project interpreter; and
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
