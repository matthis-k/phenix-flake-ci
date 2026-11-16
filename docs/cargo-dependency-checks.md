# Shared Cargo dependency checks (opt-in)

`mkCargoDependencyChecks` composes Crane's dependency-only build and
independently failing Cargo test checks. It is **not** the CI scheduler:
Nix remains the sole authority for dependency ordering, deduplication,
`--keep-going` and final evidence. Other projects need not use Cargo.

## Minimal API

```nix
let
  lib = pkgs.lib;
  cargo = phenix-flake-ci.lib.mkCargoDependencyChecks {
    inherit pkgs;
    craneLib = crane.mkLib pkgs; # supplied by the consumer, not bundled
    src = ./.;                  # complete Cargo workspace, not a subset
    lockFile = ./Cargo.lock;
    groups = {
      library = {
        packages = [ "example-library" ];
      };
      integration = {
        packages = [ "example-integration" ];
        profile = "default";
      };
      no-default = {
        packages = [ "example-library" "example-integration" ];
        profile = "no-default";
      };
    };
  };
in {
  checks.${system} = cargo.checks;
  packages.${system} = cargo.packages;
}
```

Default `profiles`: `default = [ "--all-targets" ]`; `no-default =
[ "--no-default-features" "--lib" "--tests" ]`. A consumer may override
profiles explicitly, including domain-specific feature configurations.
Each used profile gets exactly one common Crane `buildDepsOnly` derivation;
each named group gets its own `cargoTest` check with `--locked` and
`--no-fail-fast`. A failed real group need not block unrelated groups;
a genuinely shared broken dependency naturally blocks its dependents.

## Target-specific checks on one dependency profile

When a profile selects `--all-targets`, Cargo still does **not** run
Rustdoc tests. Add a separate target-only group instead of introducing a
duplicate compilation profile:

```nix
groups = {
  graph = {
    packages = [ "example-graph" ];
  };
  "graph-doc" = {
    packages = [ "example-graph" ];
    targets = [ "--doc" ];
  };
  "graph-no-default-doc" = {
    packages = [ "example-graph" ];
    profile = "no-default";
    targets = [ "--doc" ];
  };
};
```

`targets` replaces **only the target-selection flags** of the referenced
profile; common feature flags such as `--no-default-features` are retained.
For unusual test filters, provide extra `args`. Do not use target overrides
to change dependency feature resolution; define another explicit profile
for incompatible feature flags.

## Narrow rebuild inputs without changing workspace identity

```nix
groupSource = memberPaths: lib.fileset.toSource {
  root = ./.;
  fileset = lib.fileset.unions
    (map (path: ./. + "/${path}") memberPaths);
};
groups = {
  graph = {
    packages = [ "example-graph" ];
    src = groupSource [ "crates/graph" "crates/core" ];
  };
  storage = {
    packages = [ "example-storage" ];
    src = groupSource [ "crates/storage" "crates/core" ];
  };
};
```

The `src` of the **helper** must contain the complete original Cargo
workspace (all members' manifests and the matching `Cargo.lock`).
An optional **group** `src` contains real source files only for the
selected members **and all of their local path dependencies**, including
dev, build, target-specific and generated-code inputs where applicable.
The helper overlays them onto Crane's complete dummy workspace, so Cargo
still sees the original full workspace and lockfile. The group source
does **not** need to include the root Cargo.toml or Cargo.lock.

There is no automatic path-dependency closure inference today; callers
must not pass an incomplete subset, because dummy-source path
dependencies could otherwise be used instead of the real ones.
For unfamiliar workspace layouts, omit group-level `src` and retain
correct full-workspace source behavior. This spends extra invalidation
work but preserves correctness.

**Do not** rewrite `[workspace].members`, regenerate a subset manifest,
or disable `--locked`. The previous experimental API accepted
`postUnpack` for that purpose; it has been removed because a narrowed
workspace can require a different lockfile.

## Operational tradeoffs

- `buildDepsOnly` shares *compatible* registry and workspace dependency
  artifacts, not guaranteed every final Rust compilation. Different
  features, toolchains, flags, and target profiles may still rebuild.
- A shared dependency failure affects the dependent test groups, while
  failures compiling real package source should remain isolated.
- Terminal test checks do not export a redundant multi-gigabyte Cargo
  target archive. The common dependency-only derivation owns reuse.
- Source-scoped groups improve incremental invalidation but demand
  accurate path-dependency closure declarations. Full-source groups are
  the conservative default.
- Other Nix, Python, C++, mixed-language and very small projects can use
  ordinary flake `checks` and `mkFlakeCiWorkflows` without this adapter.

## Verification

Framework tests: `tests/cargo-deps-consumer`, including independent
failure injection and a check that a changed sibling crate does not
invalidate the shared dependencies or an unrelated test group.

A real multi-crate consumer is validated separately; CI success on the
small fixture does not establish workload-level performance, compatible
Cargo feature closure, or universal lockfile safety. Benchmark cold and
warm pinned revisions with check-inventory parity, runner-minutes and
actual Cargo compilation evidence before replacing an existing suite.
