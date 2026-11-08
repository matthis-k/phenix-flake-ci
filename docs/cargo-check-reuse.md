# Reuse Cargo compilation across Nix check groups

Separate Nix derivations that each invoke \`cargo test\` compile the same
Rust crates repeatedly even when Nix schedules derivations only once.
Single-store scheduling cannot deduplicate **different** derivations.

\`mkCargoTestChecks\` compiles all test executables **once per compatible
Cargo profile**. Every named check is a small Nix derivation executing selected
already-built binaries without Cargo. It returns ordinary \`checks\` and
\`packages\`; the generic planner still discovers all checks from
\`checks.<system>\` and works unchanged with matrix, local DAG and remote DAG.

\`\`\`nix
let
  cargo = phenix-flake-ci.lib.mkCargoTestChecks {
    inherit pkgs;
    src = ./.;
    lockFile = ./Cargo.lock;
    groups = {
      storage.packages = [ "phenix-storage" "phenix-schema" ];
      graph.packages = [ "phenix-graph" "phenix-plugin" ];
      kernel = {
        packages = [ "phenix-kernel" ];
        targets = { lib = true; tests = [ "kernel" "scoped_storage" ]; };
      };
      derived = {
        packages = [ "phenix-storage-graph" ];
        targets.tests = [ "derived" "derived_plugins" ];
      };
      "no-default".profile = "no-default";
    };
  };
in {
  checks.${system} = { /* other checks */ } // cargo.checks;
  packages.${system} = { /* other packages */ } // cargo.packages;
}
\`\`\`

### Minimal API: automatic per-crate checks

Omit `groups` to discover Cargo workspace members once, then emit one
`cargo-<package>` check per package. Add `noDefaultFeatures = true;` to
also compile and execute the all-workspace no-default profile. This is the
recommended default for consumers that don't need hand-selected group names.

```nix
cargo = phenix-flake-ci.lib.mkCargoTestChecks {
  inherit pkgs;
  src = lib.cleanSource ./.;
  lockFile = ./Cargo.lock;
  noDefaultFeatures = true;
};
```

Consumers attach `cargo.checks` and `cargo.packages` to their flake outputs.
The check inventory is derived from Cargo's workspace manifest, not copied into
a second CI registry. Advanced consumers may pass explicit `groups` instead.

Default compilation uses \`cargo test --workspace --all-targets --no-run\`.
The optional \`no-default\` profile uses \`cargo test --workspace
--no-default-features --lib --tests --no-run\`. Only profiles referenced by a
group are built. Supply custom named profiles explicitly via \`profiles\`;
profile selections are never implicitly merged.

A group without \`packages\` selects all workspace packages; without
\`targets\`, it executes all compiled test targets for those packages.
\`targets.lib = true\` selects library unit tests;
\`targets.tests = [ "integration_name" ];\` selects integration binaries.
Unknown packages and test names fail closed. All selected test binaries are
attempted even if a previous binary failed.

The helper exposes \`prebuildTargets\` for its compiler profiles (for example
\`.#packages.x86_64-linux.cargo-tests-default\`). Matrix consumers put those
ordinary Nix installables into \`ci/flake-check-config.nix\`'s
\`prebuildTargets\` field for shared cache publication. Single-store DAG
backends need no separate prebuild: their check derivations naturally share
the same corpus dependency.

## Ownership and limits

- Core CI owns scheduling, caching and exact-head acceptance. The Cargo
  adapter owns compilation and binary selection; the consumer owns the
  semantic groups and feature/profile boundaries.
- One workspace-wide compilation unifies Cargo feature resolution relative
  to per-crate test invocations; preserve old checks until parity is verified.
- Whole-workspace compilation broadens source invalidation. Narrowing the
  source filter later must retain manifests, lockfile, and test fixtures.
- \`cargo test --no-run\` cannot run doctests. Use a separate doctest check
  where shipping requires documentation tests.
- Clippy and release smoke use different compiler flags and should not be
  claimed as interchangeable with test binaries.
- Groups can deliberately overlap; for zero duplicate execution, assign
  each test target to only one group.

## Complete workspace API (recommended)

`mkCargoWorkspace` composes the reusable test-check engine with conventional
strict Clippy, Cargo formatting, optional Nix formatting, and release-example
smoke packages. The result still contains only standard `checks`, `packages`
and `prebuildTargets`; no custom CI runner or imperative consumer orchestration.

```nix
let
  rust = phenix-flake-ci.lib.mkCargoWorkspace {
    inherit pkgs;
    src = lib.cleanSource ./.;
    lockFile = ./Cargo.lock;
    noDefaultFeatures = true;
    nixfmtFiles = [ ./flake.nix ./ci/flake-check-config.nix ];
    smoke.kernel-smoke = {
      package = "phenix-kernel";
      example = "basic";
      # release = true; # default
    };
  };
in {
  checks.${system} = { /* project-specific checks */ } // rust.checks;
  packages.${system} = rust.packages;
}
```

The consumer owns which example demonstrates shipping behavior, while Flake
CI owns the Nix builders, Cargo lock vendoring, test corpus reuse, command
invocations and output evidence. Cargo tests and linting intentionally remain
separate profiles; their compiler flags and behavior are not interchangeable.

`cargoFmt` and `clippy` default to enabled and can be turned off
independently. `testGroups` overrides the automatic per-crate test checks
when a project needs explicit suites. The check names are ordinary Nix
attribute names, so the existing generic scheduler and cache continue to work.
