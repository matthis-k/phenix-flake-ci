# Phenix Flake CI

`phenix-flake-ci` provides a declarative CI and maintenance model for Phenix flakes.

`mkCi` defines semantic CI suites. `mkMaintenance` remains the generic command graph for repository-specific validation, fixes, and hooks.

## Semantic phases

`mkCi` classifies suites with five phase names:

| Phase | Contract |
| --- | --- |
| `build` | Produce deployable build state or artifacts. |
| `test` | Run code-level tests. |
| `runtime` | Exercise one built program through its runtime boundary. |
| `integration` | Exercise interaction between independently built components. |
| `product` | Exercise supported product or user journeys. |

The phase is classification. It does not impose execution order.

Cargo's `tests/` target kind does not decide the Phenix phase. A Cargo integration-test target can still be a `test` suite when it only tests code-level behavior.

## CI as a dependency DAG

Each semantic suite is an independent CI node. GitHub renders each node as a separate job, so independent suites use separate runners and execute concurrently.

Dependencies use `needs` with `phase.suite` references:

```nix
ciCommands = ciLib.mkCi {
  build.rust = {
    name = "Rust workspace";
    runtimeInputs = pkgs: [ pkgs.cargo ];
    exec = "cargo build --workspace --locked --quiet";
  };

  test.unit = {
    name = "Rust unit tests";
    runtimeInputs = pkgs: [ pkgs.cargo ];
    exec = "cargo test --workspace --lib --bins --locked --quiet";
  };

  test.docs = {
    name = "Rust doc tests";
    runtimeInputs = pkgs: [ pkgs.cargo ];
    exec = "cargo test --workspace --doc --locked --quiet";
  };

  product.package = {
    name = "Package smoke";
    needs = [ ];
    cache = false;
    exec = "nix build --no-link .#checks.x86_64-linux.package-smoke";
  };
};
```

When at least one `build` suite exists, non-build suites depend on every build suite by default. Set `needs = [ ]` when a suite is actually independent of build output. Explicit dependencies replace that default.

This produces a graph such as:

```text
                  test.unit
                /
             build ─── test.docs
                \
                  runtime.cli

product.package  # independent and starts immediately
```

Sibling jobs do not wait for each other. A failed sibling does not cancel unrelated jobs. The generated final gate waits for every CI job and reports failure if any required job failed or was skipped.

`maintenance ci-plan` emits the semantic DAG as NDJSON.

## Shared build state

Separate GitHub jobs do not share a filesystem. `mkCi.ci.cache` can use GitHub Actions cache to transfer reusable build state from prerequisite jobs to dependent jobs:

```nix
ci = {
  env = {
    CARGO_HOME = "\${{ runner.temp }}/cargo-home";
    CARGO_TARGET_DIR = "\${{ runner.temp }}/cargo-target";
    CARGO_TERM_QUIET = "true";
  };

  cache = {
    paths = [
      "\${{ runner.temp }}/cargo-home"
      "\${{ runner.temp }}/cargo-target"
    ];
    key = "rust-\${{ runner.os }}-\${{ github.sha }}";
    restoreKeys = [ "rust-\${{ runner.os }}-" ];
  };
};
```

The build job restores an older compatible cache when available, produces the current build state, and saves the exact commit key at job completion. Dependent jobs start after the build job and restore that exact state.

Set `cache = false` on suites that do not benefit from the shared cache. This avoids paying transfer cost for independent Nix/package jobs.

## JSON execution index

Every generated maintenance executable carries a JSON index derived from the command DAG, CI declaration, and git-hook declaration. The executable reads that index to resolve machine invocations to the exact implementation program and arguments in the Nix store.

Inspect the complete root index with `jq`:

```console
maintenance index | jq '.commands["test/rust"]'
```

A command entry contains its stable ID, path, children, dependencies, and exact execution target:

```json
{
  "id": "fix",
  "path": ["fix"],
  "kind": "exec",
  "children": [],
  "dependencies": [],
  "execution": {
    "program": "/nix/store/...-maintenance-impl/bin/maintenance-impl",
    "args": ["fix"]
  }
}
```

The root index also exposes CI jobs and hook targets. For example:

```console
maintenance index | jq '.ci.jobs'
maintenance index | jq '.hooks["pre-commit"]'
```

Machine integrations invoke a command by feeding one JSON object to `invoke`:

```console
printf '%s\n' '{"command":"fix"}' | maintenance invoke
```

An invocation may include string arguments and source metadata:

```json
{
  "command": "fix",
  "args": [],
  "source": {
    "type": "git-hook",
    "hook": "pre-commit"
  }
}
```

Generated git hooks and GitHub workflows are thin adapters. They feed an invocation into the command-scoped executable instead of embedding command execution logic. A scoped executable indexes only the commands in its dependency closure, so it cannot select unrelated commands or pull their tooling into the closure.

## Local execution

The direct command vocabulary remains phase-oriented:

```console
maintenance build
maintenance test
maintenance runtime
maintenance integration
maintenance product
maintenance pipeline
```

Run one suite:

```console
maintenance test unit
```

List suites:

```console
maintenance test --list
```

`maintenance pipeline` is intentionally a complete local diagnostic command. It runs declared phases serially, runs all suites even after failures, and emits one final aggregate status. GitHub does not use this serial pipeline for scheduling.

## NDJSON reporting

Semantic suites emit newline-delimited JSON. Successful suite output is hidden by default:

```json
{"type":"suite","phase":"test","suite":"unit","name":"Rust unit tests","status":"pass"}
```

Failures include the subprocess exit code and complete captured output:

```json
{"type":"suite","phase":"test","suite":"unit","name":"Rust unit tests","status":"fail","exit_code":101,"output":"error: ...\n"}
```

Use `--verbose` to include successful output while preserving valid NDJSON:

```console
maintenance test --verbose unit
```

Use native quiet modes such as Cargo `--quiet` as well. The outer reporter still preserves detailed failure output.

## Combining CI and maintenance

`mkCi` returns a normal maintenance command tree:

```nix
let
  ciCommands = ciLib.mkCi {
    build.rust.exec = "cargo build --workspace --quiet";
    test.rust.exec = "cargo test --workspace --quiet";
  };

  maintenance = ciLib.mkMaintenance {
    name = "maintenance";

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
        runtimeInputs = pkgs: [ pkgs.nixfmt ];
        exec = "nixfmt --check .";
      };

      fix = {
        runtimeInputs = pkgs: [ pkgs.nixfmt ];
        exec = "nixfmt .";
      };
    };
  };
in
maintenance
```

Generic maintenance commands may still set `ci.enable = true`, `ci.stage`, `ci.needs`, runner, timeout, environment, and display metadata directly.

A command that invokes another maintenance command must declare that relationship through `dependencies`. Command-scoped packages include only the selected command, its aggregate children, and declared dependencies.

## Generated GitHub workflow

When `ci.github.enable = true`, the workflow is generated from the maintenance graph. Each CI job feeds a JSON invocation into a command-scoped flake app with `nix run --quiet`.

Consumers should commit the generated workflow and keep it synchronized with the Nix declaration.
