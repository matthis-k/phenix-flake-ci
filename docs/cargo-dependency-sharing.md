# Cargo dependency compilation sharing (opt-in)

## Invariants

The Nix derivation graph is the only build scheduler. Consumers declare normal
`checks`; each test check has its own independently failing derivation. Passing
test evidence remains exact-head and incomplete outputs fail closed. No
whole-workspace test-binary corpus is a prerequisite of every test check.

## Design choice

| Implementation | Compiled reuse | Independent package error |
| --- | --- | --- |
| Separate Cargo invocation per check (current baseline) | Reuses exact Nix store paths, not overlapping Cargo compilation | Isolated |
| One workspace `cargo test --no-run` corpus (draft #51) | Maximal test executable reuse | One crate failure blocks all checks |
| Dependency-only corpus + independent `cargoTest` checks (this adapter) | Shares compiled dependency artifacts for compatible Cargo profiles | Real package source errors stay in the affected check |

Use Crane's existing `buildDepsOnly` and `cargoTest` implementation rather
than implementing our own Cargo target cache, path rewriting, fingerprint
tracking, or dependency scheduler. See https://crane.dev/API.html.

`mkCargoDependencyChecks` requires `craneLib` supplied by the consumer, a
source workspace, its lockfile, and named test groups. Each profile creates
one dummy-source dependency derivation; each group becomes a separate real
`cargoTest` check depending on its profile's immutable dependency artifacts.
Each group may set `src` and `postUnpack` to supply a **source-filtered
workspace** and its matching reduced Cargo workspace manifest. Without that
scoping, a full-workspace source change invalidates every test derivation even
when most crates are unaffected. This is a correctness requirement for
incremental work, not a cosmetic optimization. Only the dependency-only corpus
uses the full workspace's manifests.

The real `cargoTest` check leaves explicitly disable
`doInstallCargoArtifacts`: they run tests but do not persist a redundant
multi-gigabyte Cargo `target` archive into each Nix output. Only the
`buildDepsOnly` derivations export a compiler artifact cache for reuse. The
library's fixture checks that leaf outputs contain no `target.tar.zst`.

The optional `ci-prebuild-cargo-deps-<profile>` package alias is ordinary Nix
output, not a new scheduler queue.

Example:

```nix
let
  suite = phenix-flake-ci.lib.mkCargoDependencyChecks {
    inherit pkgs;
    craneLib = crane.mkLib pkgs;
    src = ./.;
    lockFile = ./Cargo.lock;
    groups = {
      graph.packages = [ "phenix-graph" "phenix-plugin" ];
      storage.packages = [ "phenix-schema" "phenix-storage" ];
      no-default = {
        profile = "no-default";
        packages = [ "phenix-schema" "phenix-storage" "phenix-graph" "phenix-plugin" ];
      };
    };
  };
in {
  checks.${system} = suite.checks;
  packages.${system} = suite.packages;
}
```

Default feature-profile arguments target `--all-targets`; no-default uses
`--no-default-features --lib --tests`. Crane itself adds `--all-targets`
to dependency preparation, so target-selection flags are not passed there a
second time. A mismatch between effective feature unification/target flags in
dependency and group builds may still trigger Cargo recompilation. Release
smoke binaries are a separate compilation profile. Clippy is not moved into
the shared test compilation until verified separately.

## Required test evidence

The two-crate fixture in `tests/cargo-deps-consumer` uses two separate,
source-filtered group derivations with a real shared `once_cell` registry crate.
It builds both groups, modifies only one crate's Rust source to contain a
genuine compile error, then verifies four conditions:

1. The shared dependency derivation identity is unchanged.
2. The unaffected test group's derivation identity is unchanged.
3. The edited test group's derivation identity changes and Nix `--keep-going`
   returns failure because its actual Rust source cannot compile.
4. The unaffected group remains buildable from the local store with no
   substitution or recompilation.

That demonstrates source-level failure isolation and derivation-level sharing,
not an end-to-end speedup. A real workspace benchmark must compare cold
compilation, warm cache reuse, compiler invocations, Nix closure transfer,
critical-path duration and runner cost against the baseline at matching
inputs and toolchains. Do not merge this opt-in adapter based on fixture
success alone.

## Known boundaries

- A dependency shared by *both* test groups may legitimately block both when
  it fails. This is a real dependency edge, not an isolation failure.
- Cargo feature unification and rustc fingerprints are not abstracted away.
- Shared dependency builds can enlarge the first build when only one group is
  requested; per-package dependency caches are a possible alternative, but
  create more derivations and repeat external dependency compilation.
- Nix binary caches can distribute the shared outputs between branches and
  machines. GitHub Actions cache branch restrictions still apply.
- This adapter is explicitly optional; default `checks`, `mkTest`,
  `mkSuite`, local `nix run .#ci`, and the trusted/untrusted workflow
  security boundary are unchanged.
