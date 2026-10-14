# Existing Nix tools and Phenix CI

Phenix CI coordinates builds declared by a consumer flake. It does not define
the consumer's packages or tests, compile source code, or replace Nix's
derivation scheduler. The initial contract covers `checks.<system>.*`.
A check can depend on package builds, formatter checks, Rust tests, integration
tests, or other Nix derivations.

## Interface

A consumer keeps one flake check configuration and chooses an execution backend:

```nix
ci = {
  backend = "nix-remote";
  remote = {
    transport = "tailscale";
    # Omit workers to use the measured four-worker default.
    slotsPerWorker = 2;
    credentials = "oidc";
  };
};
```

`mkFlakeCiWorkflow` renders a small pinned GitHub Actions caller from this
policy. The generic planner discovers the actual flake `checks`. Neither
file lists test names. The same check derivations can run locally with
`nix flake check`. Backend-specific artifact handling stays behind the
existing reusable workflows.

The backend choice is not a choice of compiler, package manager, test
framework or formatter. Those belong to the consumer flake.

## Established projects

| Project | Existing capability | How Phenix CI uses it |
| --- | --- | --- |
| [flake-parts](https://github.com/hercules-ci/flake-parts) | Modules and `perSystem` flake outputs | Read the resulting `checks` without requiring consumers to rewrite their modules |
| [treefmt-nix](https://github.com/numtide/treefmt-nix) | Formatter and formatting check derivations | Build the standard check output; leave formatting rules in treefmt |
| [git-hooks.nix](https://github.com/cachix/git-hooks.nix) | pre-commit and sandboxed hook checks | Build hook checks exported by the flake; keep local hooks owned by git-hooks |
| [nix-github-actions](https://github.com/nix-community/nix-github-actions) | Flake attribute-to-GitHub matrix conversion | Compare its native matrix approach with Phenix's shared-cache and DAG approach; do not copy its attribute mapping logic |
| [nix-fast-build](https://github.com/Mic92/nix-fast-build) | Parallel evaluation and builds, `--skip-cached`, optional remote execution | Benchmark as an optional local or CI execution engine; avoid implementing a second evaluator |
| [nix-eval-jobs](https://github.com/NixOS/nix-eval-jobs) | Streamable parallel Nix evaluation | Prefer the established evaluator if evaluation becomes a measured bottleneck |
| [github-actions-nix](https://github.com/synapdeck/github-actions-nix) and [actions.nix](https://github.com/nialov/actions.nix) | Nix-defined GitHub Actions workflows | Consider typed workflow adapters; retain a minimal pinned caller for consumers without either module |
| [cache-nix-action](https://github.com/nix-community/cache-nix-action) | Restore and save GitHub-hosted Nix stores | Continue using the existing action for the matrix and staged DAG backends |
| [Cachix](https://www.cachix.org/) and [Attic](https://github.com/zhaofengli/attic) | Binary cache services | Use as substituters and publishers through Nix; do not implement a cache protocol |
| Native Nix `ssh-ng` | Distributed derivation builds and store transfers | Keep one Nix coordinator for the Tailscale backend; Phenix manages worker lifecycle and verifies store readiness |

The table describes integration boundaries, not dependencies bundled into
Phenix CI. Projects keep their existing inputs. No extra flake input is
required just to use `mkFlakeCiWorkflow`.

## flake-parts example

For a consumer that already uses flake-parts:

```nix
# Within the existing flake-parts mkFlake module:
{ inputs, ... }: {
  systems = [ "x86_64-linux" ];

  perSystem = { pkgs, ... }: {
    checks.smoke = pkgs.runCommand "smoke" { } ''
      test -n "ok"
      touch "$out"
    '';
  };

  flake.lib.flakeCiWorkflow = inputs.phenix-flake-ci.lib.mkFlakeCiWorkflow {
    ci = {
      backend = "nix-remote";
      remote.credentials = "oidc";
      # workers defaults to 4; slotsPerWorker defaults to 2.
    };
    configPath = "ci/flake-check-config.nix";
    libraryRef = inputs.phenix-flake-ci.rev;
  };
}
```

Use the locked flake input's `rev` as the GitHub workflow pin. It is one
source for the library code and the reusable workflow. For a local input
without `rev`, use a tested immutable commit explicitly. Render
`nix eval --raw .#lib.flakeCiWorkflow` to the tracked workflow file.
This option is a plain flake attribute; the consumer does not need to
replace its flake-parts modules.

Tools such as treefmt-nix and git-hooks.nix can populate `checks` instead
of the `checks.smoke` example. Phenix CI then discovers and runs them
through the same evaluator and shipping gate.

## Keep one owner for each responsibility

- **Flake** owns the derivations, architecture variants and test definitions.
- **Nix** owns dependency evaluation, store identities, sandboxing, substitution and build scheduling.
- **Existing tools** own formatting, Git hooks, language-specific tests and binary cache implementations.
- **Phenix CI** owns target discovery, optional scheduling hints, selection of the execution backend, runner setup and complete per-commit evidence.
- **GitHub Actions** owns the job graph, credentials, approvals and artifacts.

The existing Rust strategy is a scheduling adapter. It supplies hints and
prebuild targets, not a second Rust build system. Custom strategies should
use the same generic check-plan interface.

## Next compatibility work

The current implementation deliberately does not accept arbitrary flake
output trees. Adding those requires an explicit target policy rather than
converting unrelated packages or development shells into shipping checks.

1. **Keep `checks` authoritative.** Support flake-parts, treefmt and hooks
   immediately through their standard outputs. Preserve all shipping checks.
2. **Compare established evaluators.** Benchmark `nix-fast-build` /
   `nix-eval-jobs` against native `nix build` on exactly the same roots,
   cold and warm, before adding another engine.
3. **Extend target discovery only when needed.** Add opt-in `packages` and
   `devShells` selection with flake-native names and separate output evidence.
   Never silently promote or discard existing `checks`.
4. **Make workflow rendering replaceable.** Offer a Nix attribute-set adapter
   for existing workflow generators before expanding the thin YAML renderer.
   Do not build a general-purpose GitHub Actions DSL inside Phenix CI.
5. **Leave cache protocols outside the library.** Benchmark a shared
   substituter such as Cachix or Attic against GitHub cache restoration,
   including transfer time, cache misses and runner-minutes.

The 13 October 2026 consumer benchmark supports **four Tailscale workers**
as the latency/cost default. That result does not establish a speedup for
other flakes, worker sizes or cache providers. Keep canonical CI required
until the consumer validates parity and repeatability after choosing a
different backend.
