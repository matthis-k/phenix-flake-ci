# Phenix Flake CI

`phenix-flake-ci` is the shared declarative maintenance and CI library for Phenix flakes.

It has two layers. `mkCi` defines the normal CI lifecycle. `mkMaintenance` remains the generic command graph for repository-specific checks, fixes, and hooks.

## Flake input

```nix
inputs.phenix-flake-ci.url = "github:matthis-k/phenix-flake-ci";
```

The library is available as `inputs.phenix-flake-ci.lib`.

## Semantic CI

`mkCi` has five phases. Omit phases that do not apply.

| Phase | Contract |
| --- | --- |
| `build` | Produce deployable artifacts. |
| `test` | Run code-level tests such as unit and component suites. |
| `runtime` | Start built artifacts and test their external behavior. |
| `integration` | Exercise interactions between independently built artifacts. |
| `product` | Exercise supported user journeys. |

Cargo's test target type does not decide the Phenix phase. A Rust test under `tests/` can still be part of `test`. Use `integration` when the test crosses built program or service boundaries.

Declare suites, not individual test cases:

```nix
ciCommands = ciLib.mkCi {
  build.rust = {
    name = "Rust workspace";
    runtimeInputs = pkgs: [ pkgs.cargo ];
    exec = "cargo build --workspace --locked --quiet";
  };

  test.rust = {
    name = "Rust tests";
    runtimeInputs = pkgs: [ pkgs.cargo ];
    exec = "cargo test --workspace --locked --quiet";
  };

  runtime.cli = {
    name = "CLI startup";
    exec = "./result/bin/phenix --version";
  };
};
```

The generated command vocabulary is direct:

```console
maintenance build
maintenance test
maintenance runtime
maintenance integration
maintenance product
```

Run one suite by name:

```console
maintenance test rust
```

List the suites in a phase:

```console
maintenance test --list
```

Use `--verbose` when the underlying command output is useful:

```console
maintenance test --verbose
maintenance test --verbose rust
```

### One build, later verification

The generated GitHub projection keeps the semantic phases as ordered steps in one job by default:

```text
Build -> Test -> Runtime -> Integration -> Product
```

That is deliberate. GitHub jobs use separate runners. Keeping these phases in one job preserves the Nix store and working tree between them, so a later phase can use the exact artifacts produced by `build` instead of reconstructing them on another runner.

A normal application stays separate. `nix run .#my-app` builds and runs that app. It does not invoke the semantic CI commands.

### Suite output

Suites are quiet by default. Successful process output is discarded. A successful suite prints one line:

```text
PASS Rust tests
```

A failed suite prints its status and the captured command output:

```text
FAIL Rust tests
error: ...
```

All suites in a phase run before the phase returns failure. This gives one concise result per suite while preserving the full error output for failed suites.

Use native quiet flags as well. For Cargo that usually means `--quiet`. The outer reporter still captures remaining success noise and exposes failure output consistently across tools.

## Combining semantic CI and maintenance

`mkCi` returns a normal maintenance command tree. Add repository-specific commands with an attribute-set merge:

```nix
let
  ciCommands = ciLib.mkCi {
    build.rust = {
      runtimeInputs = pkgs: [ pkgs.cargo ];
      exec = "cargo build --workspace --locked --quiet";
    };

    test.rust = {
      runtimeInputs = pkgs: [ pkgs.cargo ];
      exec = "cargo test --workspace --locked --quiet";
    };
  };

  maintenance = ciLib.mkMaintenance {
    name = "maintenance";
    description = "Repository maintenance";

    ci.github = {
      enable = true;
      outputName = "phenix-maintenance";
    };

    gitHooks = {
      enable = true;
      preCommit = [ "fix" ];
    };

    commands = ciCommands // {
      check-format = {
        description = "Check Nix formatting";
        runtimeInputs = pkgs: [ pkgs.nixfmt ];
        exec = "nixfmt --check .";
      };

      fix = {
        description = "Apply deterministic formatting";
        runtimeInputs = pkgs: [ pkgs.nixfmt ];
        exec = "nixfmt .";
      };
    };
  };

  outputs = ciLib.mkMaintenanceOutputs {
    inherit maintenance;
    systems = [ system ];
    pkgsFor = _: pkgs;
    outputName = "phenix-maintenance";
  };
in
{
  packages = outputs.packages.${system};
  apps = outputs.apps.${system};
}
```

GitHub job metadata is shared by the semantic phases because they intentionally run on one runner. Configure it through `mkCi.ci`:

```nix
ciCommands = ciLib.mkCi {
  ci = {
    name = "Rust";
    runner = "ubuntu-latest";
    timeoutMinutes = 60;
    env.CARGO_TERM_QUIET = "true";
  };

  build.rust = { ... };
  test.rust = { ... };
};
```

## Generic command graph

`mkMaintenance` still supports arbitrary command trees. Each command can set `ci.enable = true`. `ci.stage` groups enabled commands into GitHub jobs. Runner, timeout, job dependencies, environment, and display names stay beside the command declaration.

A command that invokes another maintenance command must declare that relationship with `dependencies`:

```nix
commands = {
  check.rust = {
    runtimeInputs = pkgs: [ pkgs.cargo ];
    exec = "cargo check --workspace";
  };

  rust-ci.clippy = {
    dependencies = [ [ "check" "rust" ] ];
    runtimeInputs = pkgs: [ pkgs.cargo pkgs.clippy ];
    exec = ''
      maintenance check rust
      cargo clippy --workspace --all-targets -- -D warnings
    '';
  };
};
```

A scoped package contains the selected command, descendants that an aggregate command executes, and declared dependencies. Missing dependencies and dependency cycles fail during evaluation. Runtime inputs are deduplicated before package construction.

## Git hooks

Git hooks are disabled unless `gitHooks.enable = true` is declared. `gitHooks.preCommit` is a path into the maintenance command tree, for example `[ "fix" ]` or `[ "check" "format" ]`.

When enabled, `mkMaintenancePackage` exposes a `shellHook`. The installed pre-commit hook executes a package scoped to `gitHooks.preCommit`. The hook re-stages only paths that were staged before maintenance ran, then checks the staged diff.

## Generated GitHub workflow

When `ci.github.enable = true`, the library renders the GitHub Actions workflow from the maintenance graph. Each CI step invokes the command-scoped flake app generated for that command path.

Consumers should commit the generated workflow and verify that it stays synchronized. The compatibility dispatcher remains available for development shell use:

```console
nix run .#phenix-maintenance -- test
```
