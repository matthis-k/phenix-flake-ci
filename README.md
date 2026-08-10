# Phenix Flake CI

`phenix-flake-ci` is the shared declarative maintenance/testing library for Phenix flakes.

A repository declares one command tree. The library materializes that tree as a shell application, exposes selected commands as CI stages/steps, can render the committed GitHub Actions workflow, and can install an opt-in pre-commit hook that invokes the same command implementation.

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
        exec = "nix flake check --no-build";
      };

      fix = {
        description = "Deterministic normalization";
        exec = "nixfmt .";
      };
    };
  };

  materialized = ciLib.mkMaintenancePackage {
    inherit pkgs maintenance;
  };
in {
  packages.phenix-maintenance = materialized.package;
  apps.phenix-maintenance = materialized.app;

  devShells.default = pkgs.mkShell {
    packages = [ materialized.package ];
    shellHook = materialized.shellHook;
  };
}
```

## Git hooks

Git hooks are disabled unless `gitHooks.enable = true` is declared. `gitHooks.preCommit` is a path into the maintenance command tree, for example `[ "fix" ]` or `[ "check" "format" ]`.

When enabled, `mkMaintenancePackage` exposes a `shellHook`. A consumer can append that hook to its development shell. It installs the generated pre-commit hook into the repository's Git directory and sets the local `core.hooksPath` accordingly.

The hook records the paths staged before maintenance runs, executes the configured maintenance command, re-stages only those paths, and runs `git diff --cached --check`. Outside the development shell it falls back to `nix develop --command ...`, so the repository does not need to carry a separate `.githooks` implementation.

## CI model

Each command can set `ci.enable = true`. Enabled commands become visible CI steps. `ci.stage` groups steps into jobs; runner, timeout, dependencies, environment, and display names are declared alongside the command.

When `ci.github.enable = true`, the library renders the GitHub Actions workflow from the same command graph. Consumers should commit that generated projection and validate that it remains synchronized.
