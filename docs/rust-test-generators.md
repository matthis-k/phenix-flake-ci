# Rust test generation from Nix declarations

`mkRustTests` is a thin **Nix generator**, not a test runner. The consumer selects Cargo packages and a compilation topology; Cargo discovers their test targets. `mkCargoTestPackage` produces precompiled executables; Flake CI discovers the returned `packages.${system}."test.*"` and runs them *after* the existing build barrier.

## One crate

```nix
rust = phenix-flake-ci.lib.mkRustTests {
  inherit pkgs;
  src = ./.;
  lockFile = ./Cargo.lock;
  crate = "phenix-graph";
};

packages.${system} = rust.packages;
```

This generates `packages.${system}."test.rust-phenix-graph"`. The native Nix coordinator builds the artifact once, then the runtime phase executes the compiled test binaries without invoking Cargo. **A test failure is a runtime failure**, while a compilation error is a Nix build failure.

## Multiple crates: choose the build strategy

```nix
tests = phenix-flake-ci.lib.mkRustTests {
  inherit pkgs;
  src = ./.;
  lockFile = ./Cargo.lock;
  crates = [ "phenix-graph" "phenix-storage" ];
  strategy = "shared"; # or "isolated" (default with explicit crates)
  cargoArgs = [ "--features" "extras" ];
};
packages.${system} = tests.packages;
```

| Strategy | Compilation artifact | Advantage | Drawback |
| --- | --- | --- | --- |
| `isolated` | One existing `mkCargoTestPackage` derivation per crate | Independent compile failures; simple incremental crate selection | Dependencies may compile more than once across derivations |
| `shared` | One `mkCargoTestPackage` derivation, multiple per-crate run wrappers | One compatible compilation of the selected test corpus | One broken crate prevents the shared corpus from building |

The choice is a consumer policy, not a new Cargo/build engine. Rust dependency-only reuse for independently failing **build-time** test checks already exists in `mkCargoDependencyChecks`, via Crane `buildDepsOnly` and `cargoTest`; don't combine all three without measuring redundant compilation.

## Entire Cargo workspace

```nix
workspace = phenix-flake-ci.lib.mkRustTests {
  inherit pkgs;
  src = ./.;
  lockFile = ./Cargo.lock;
  # No crate/crates -> reuse mkCargoTestPackages' workspace discovery.
};
packages.${system} = workspace.packages;
```

This reuses `mkCargoTestPackages` as the *only* workspace member parser. It emits one `test.rust-<cargo-package>` Nix package per crate backed by one compiled workspace test corpus.

## What 'all tests' means here

- The generator calls Cargo `test --no-run --all-targets`; Cargo discovers and compiles applicable unit, integration, binary, example and other harness test executables.
- `rustdoc` doctests are **not** captured by Cargo's runnable `--no-run` artifact protocol. Add a separate `mkCargoDependencyChecks` group with `targets = [ "--doc" ];` when docs are required, or use a normal `cargo test --doc` check.
- Cargo test binaries often need source fixtures via `CARGO_MANIFEST_DIR`. The existing package helper executes against the immutable Nix source tree to preserve that behavior.
- Per-test-case runtime isolation within one compiled binary remains Cargo/libtest's job. Independent runtime jobs are per returned `test.*` package, not per Rust `#[test]` function.
- Workspace-wide generation relies on the existing workspace helper, which handles explicit paths and trailing `/*` globs. For unusual Cargo workspace patterns, declare crates explicitly.

No new CI cache, scheduler, runner, metadata scanner, or programming-language-specific branch in the generic CI planner is introduced.
