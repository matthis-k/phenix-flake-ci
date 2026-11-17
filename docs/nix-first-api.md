# Nix-first Flake CI facade

`mkFlakeCI` is **glue**, not a new CI engine. It generates Nix apps and GitHub workflow files using existing implementations. It does not introduce another planner, builder scheduler, cache format, test runner, YAML renderer, or Python model layer.

## Consumer flake

```nix
let
  ci = phenix-flake-ci.lib.mkFlakeCI {
    inherit pkgs;
    config = import ./ci/flake-check-config.nix;
    configPath = "ci/flake-check-config.nix";
    libraryRef = phenix-flake-ci.rev;
  };
in {
  apps.${system} = existingApps // ci.apps;
  packages.${system} = existingPackages // ci.packages;
  # Existing checks.${system} and packages.${system}."test.*" are unchanged.
}
```

`config` remains the existing `mkFlakeCheckConfig` value. GitHub policy remains the existing `ci.trusted` and `ci.pullRequest` configuration. The facade delegates validation and rendering to `mkFlakeCiWorkflows`, without defining a second schema for execution policies.

## Uniform commands

| Command | Meaning |
| --- | --- |
| `nix run .#ci` | Local exact-head parity using the existing Nix store, build DAG, barrier and runtime runner |
| `nix run .#ci-workflows` | Write the generated trusted and PR workflow files |
| `nix run .#ci-check-workflows` | Fail if the committed workflow files differ from generated definitions |
| `nix build .#ci-workflows` | Build a store directory containing the generated workflow files |

`workflowNames = { trusted = "flake-ci.yml"; pullRequest = "flake-ci-pr.yml"; };` controls filenames. For repositories that do not use generated GitHub workflows, set `github = false`; the facade will expose only `apps.ci` and `packages.ci` and requires no library commit pin.

## Where behavior actually lives

| Responsibility | Existing owner |
| --- | --- |
| Check definitions | Consumer `checks.${system}` and `packages.${system}."test.*"`; optional `mkSuite`/`mkTest` |
| Dependency scheduling and cache identities | Nix derivations, store, substituters, configured builders |
| Check inventory and exact-head plan | `flake-check-plan.py` |
| Nix build and synchronized barrier | `flake-check-dag.py` |
| Prebuilt runtime tests and evidence | `flake-test-packages.py` |
| Local orchestration | `flake-ci-local.py` |
| Tailscale Nix machine setup | `nix-remote-workers.py` |
| GitHub trust separation and workflows | `mkFlakeCiWorkflows` and existing reusable Actions workflows |
| Thin entry points and file emission | `mkFlakeCI` using Nixpkgs writeShellApplication, writeText and runCommand |

Trusted GitHub may continue using Tailscale `ssh-ng` remote Nix builders; untrusted PRs continue using the staged DAG. Both have the same check inventory and exact-head evidence protocol. The existing build-to-runtime synchronization barrier remains in place.

## Existing ecosystem libraries considered

- `pkgs.writeShellApplication`, `pkgs.writeText`, `pkgs.runCommand`: selected; built into Nixpkgs and adequate for the facade.
- `flake-parts`: optional consumer authoring structure, not required by the CI library.
- `nix-github-actions`: a flake-output-to-matrix generator, not a substitute for the existing Tailscale coordinated build and runtime evidence gate.
- `nix-fast-build`: useful optional high-performance build CLI; no need to embed another build scheduler.
- `hestia`: potentially useful alternative for hosted GitHub cache/matrix workflows, but adopting it would require verified parity for trust, barrier, and failure evidence.

## Constraints

- `config.system` must match the `pkgs` host platform.
- `configPath` identifies the same committed config that was used to construct `config`.
- Local CI currently requires a clean tracked worktree for exact-head attestation; use `nix flake check --keep-going` for uncommitted development.
- GitHub output generation requires an immutable 40-character library commit and valid trust configuration.
- Worker credentials, SSH ACLs, cache publication, and remote resources remain existing external configuration.

## Migration

Merge the generated app/package outputs into existing flake outputs; never replace the consumer's `test.*` packages or other packages.

Adopt incrementally. First generate the workflows and assert they match the existing rendered output. Then expose `apps.ci`. Never change check definitions, the GitHub backend, or the final evidence gate as part of this facade cutover. No Phenix-AI PR stack rebase is required.
