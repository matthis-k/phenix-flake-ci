# Command-scoped maintenance closures

## Problem

`mkMaintenancePackage` recursively collects `runtimeInputs` from every command and gives their union to one `writeShellApplication`.

A narrow invocation such as `maintenance fix` therefore realizes tools used only by checks, tests, sandboxing, or product validation. In `phenix-conductor`, the `fix` job recently requested 199 store paths, about 856 MiB of downloads and 2.8 GiB unpacked, even though `fix` only needs formatting and static-fix tools.

The package boundary is wrong. Runtime arguments choose the command after Nix has already realized the package closure.

## Target model

Keep one maintenance command graph and one source of command semantics. Package execution per command or CI boundary so Nix realizes only the tools that execution can reach.

The interactive dispatcher may remain available for development convenience. CI and other narrow invocations must have command-scoped apps/packages.

## Requirements

1. Generate a package/app for an executable command boundary without copying command semantics into a second definition.
2. A command-scoped package includes that command's required runtime inputs and the inputs of commands it explicitly invokes. Sibling and unrelated commands do not enter the closure.
3. Represent cross-command execution explicitly in maintenance metadata. Calls such as `rust-ci clippy -> check rust` and `rust-ci unit -> test unit` must not depend on the current monolithic package accidentally placing every tool on `PATH`.
4. Detect invalid command dependencies at evaluation time. Reject missing targets and dependency cycles.
5. Preserve the existing maintenance dispatcher for interactive use unless removing it produces a simpler compatible interface.
6. Generated GitHub Actions must invoke command-scoped apps/packages for individual CI jobs. A `fix` job must not realize test, sandbox, product, or unrelated source-check tooling.
7. Keep git-hook behavior deterministic. A hook that runs only `fix` should have a path that does not require the complete maintenance closure.
8. Deduplicate runtime inputs before package construction.
9. Keep command descriptions, ordering, CI metadata, git-hook metadata, and executable bodies in one maintenance definition.

## Acceptance criteria

- [x] Tests cover runtime-input collection for a leaf command, an aggregate command, and an explicit cross-command dependency.
- [x] Tests prove unrelated sibling inputs are absent from a command-scoped package closure.
- [x] Tests reject a dependency cycle and a dependency on a missing command.
- [x] GitHub workflow rendering targets command-scoped execution for CI jobs.
- [x] `fix` can run through a command-scoped flake app/package without realizing tools used only by `check`, `test`, or product jobs.
- [x] Existing aggregate and interactive maintenance commands still work.
- [x] README documents the command-scoped invocation form and the compatibility dispatcher.
- [x] The implementation does not duplicate command scripts or CI definitions to obtain smaller closures.

## Validation

Use Nix closure inspection in tests or fixtures where practical. Do not pin an exact byte count because nixpkgs revisions change closure sizes. Assert semantic exclusions instead, for example that the `fix` execution closure does not contain packages introduced only by sandbox tests or product builds.

Run the repository's normal checks after implementation.

## Non-goals

- Optimizing the intrinsic closure of Cargo, rustfmt, Git, or Nix packages.
- Changing consumer test semantics.
- Making one runtime-selected monolithic executable magically have different Nix closures for different arguments.

## Worker checklist

- [x] Refactor `mk-maintenance-package.nix` around command-scoped package generation.
- [x] Add explicit command dependency metadata and validation if cross-command calls require it.
- [x] Update workflow rendering to target scoped apps/packages.
- [x] Update git-hook generation to use the narrowest suitable execution path.
- [x] Add unit/evaluation coverage for dependency resolution and closure isolation.
- [x] Update README examples.
- [x] Run all repository checks and record the validation in the PR description.
