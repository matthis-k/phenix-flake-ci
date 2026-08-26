# Phenix Flake CI

`phenix-flake-ci` is the shared declarative maintenance and CI library for Phenix flakes.

A repository declares one command tree. The library renders one dispatcher for interactive use and command-scoped packages for narrow execution boundaries such as CI jobs and git hooks. Command bodies stay in one definition.

## Flake input

```nix
inputs.phenix-flake-ci.url = "github:matthis-k/phenix-flake-ci";
```

The library is available as `inputs.phenix-flake-ci.lib`.

## Basic usage

```nix
let
  ciLib = inputs.phenix-flake-ci.lib;

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

    commands = {
      check = {
        description = "Read-only checks";
        ci = {
          enable = true;
          stage = "check";
          name = "Check";
        };
        runtimeInputs = pkgs: [ pkgs.statix ];
        exec = "statix check .";
      };

      fix = {
        description = "Deterministic normalization";
        runtimeInputs = pkgs: [ pkgs.nixfmt-rfc-style ];
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
in {
  packages = outputs.packages.${system};
  apps = outputs.apps.${system};

  devShells.default = pkgs.mkShell {
    packages = [ outputs.packages.${system}.phenix-maintenance ];
    shellHook = (ciLib.mkMaintenancePackage {
      inherit pkgs maintenance;
    }).shellHook;
  };
}
```

`mkMaintenanceOutputs` exposes the compatibility dispatcher as `phenix-maintenance` and adds one hashed command-scoped output for every declared command path. Generated CI uses those scoped outputs automatically. The hash avoids collisions between command paths while keeping the command graph as the only source of semantics.

## Explicit command dependencies

A command that invokes another maintenance command must declare that relationship with `dependencies`.

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

When enabled, `mkMaintenancePackage` exposes a `shellHook`. The installed pre-commit hook executes a package scoped to `gitHooks.preCommit`, so hook execution does not need the full maintenance runtime closure. The hook re-stages only paths that were staged before maintenance ran, then checks the staged diff.

## CI model

Each command can set `ci.enable = true`. Enabled commands become CI steps. `ci.stage` groups steps into jobs. Runner, timeout, job dependencies, environment, and display names stay beside the command declaration.

When `ci.github.enable = true`, the library renders the GitHub Actions workflow from the same command graph. Each CI step invokes the command-scoped flake app generated for that command path. Consumers should commit the generated workflow and verify that it stays synchronized.

The compatibility dispatcher remains available for development shell use:

```console
nix run .#phenix-maintenance -- check rust
```

Narrow CI and automation should use the generated command-scoped outputs instead of the dispatcher.
