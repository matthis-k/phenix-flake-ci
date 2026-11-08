# Select a CI execution backend

Use `mkFlakeCiWorkflow` to generate a GitHub Actions caller from a Nix
declaration. The workflow invokes an existing reusable workflow pinned to one
immutable `phenix-flake-ci` commit. Check discovery, dependency ordering and
acceptance evidence remain with the existing backend implementations.

## Define the policy once

The configuration file is a Nix expression read by the plan job and by the
workflow generator. This minimal example can be placed at
`ci/flake-check-config.nix`:

```nix
{
  schemaVersion = 1;
  system = "x86_64-linux";
  shardCount = 4;
  ci = {
    backend = "nix-remote";
    remote = {
      transport = "tailscale";
      workers = 4;
      slotsPerWorker = 2;
      credentials = "oidc";
    };
  };
}
```

Consumers already using `mkFlakeCheckConfig` can pass the same `ci` attribute
to it. Existing consumers that omit `ci` keep their current plan format.

Expose the generated workflow from the consumer's `flake.nix`:

```nix
# Inside outputs = { phenix-flake-ci, ... }: { ... };
lib.flakeCiWorkflow =
  let
    checkConfig = import ./ci/flake-check-config.nix;
  in
  phenix-flake-ci.lib.mkFlakeCiWorkflow {
    ci = checkConfig.ci;
    configPath = "ci/flake-check-config.nix";
    libraryRef = phenix-flake-ci.rev; # Locked commit, supplied by Nix
  };
```

Generate and commit the caller file:

```sh
nix eval --raw .#lib.flakeCiWorkflow > .github/workflows/flake-ci.yml
git add .github/workflows/flake-ci.yml ci/flake-check-config.nix
```

Read `libraryRef` from the locked `phenix-flake-ci` flake input's `rev`.
This keeps the reusable workflow and library checkout on the same immutable
commit without copying a SHA into another file. The generator accepts only a
40-character lowercase Git SHA and writes both GitHub references from it.
For non-GitHub or local inputs without `rev`, pass an explicit tested commit.
Generated callers cannot select a mutable branch or tag.

## Backends

| `ci.backend` | Workflow | What runs |
| --- | --- | --- |
| `matrix` | `flake-checks.yml` | GitHub-hosted Nix check shards |
| `nix-local` | `flake-local-dag.yml` | Entire dependency DAG in one GitHub runner/Nix store (PR-only default) |
| `nix-dag` | `flake-dag-staged.yml` | Generated check DAG, staged fan-out and join |
| `nix-remote` | `flake-nix-builders.yml` | One Nix scheduler with ephemeral Tailscale `ssh-ng` workers |

Changing the backend changes only execution. The consumer flake still supplies
the entire check inventory. Each existing backend must produce complete,
exact-head evidence. This wrapper does not schedule derivations or maintain
a separate check registry.

For local verification run `nix flake check`, or build the same checks via
`nix build .#checks.x86_64-linux.<check-name>`. No GitHub or Tailscale
connection is needed for local execution.

## Remote-builder trust and configuration

The remote backend defaults to push to `main` and manual
`workflow_dispatch`. It rejects `pull_request` triggers, since untrusted
fork code must not run on trusted tailnet builders. The wrapper grants
`id-token: write` for both credential modes because the existing reusable
remote-builder workflow requests that permission. Its token is only used
when `credentials = "oidc"`.

Configure Tailscale federated identity with `TS_OAUTH_CLIENT_ID` as
a GitHub Actions secret or repository variable. The generated caller derives
`TS_AUDIENCE = api.tailscale.com/<client-id>` from that value, so no
separate audience setting is needed. Configure the `tag:phenix-ci`
tag owner and SSH grants described in
[distributed Nix builders](distributed-nix-builders.md). To use a tagged
ephemeral authentication key instead, set `credentials = "authkey"` and
provide only `TS_AUTHKEY`. The generator forwards only the selected secret
set. Never put secret values into Nix expressions or committed workflows.

The remote configuration supports 1-16 workers and 1-32 slots per worker.
It selects the existing Tailscale workflow without changing its timeout,
SSH-store checks, release handling or final acceptance job.

The `matrix` and `nix-dag` backends default to pull requests, pushes to
`main` and manual dispatch. Change event selection through the optional
`events` argument:

```nix
events = {
  pullRequest = false;
  pushBranches = [ "main" "release" ];
  workflowDispatch = true;
};
```

The remote backend always rejects `pullRequest = true`. At least one event
must remain enabled. Branch name and config path inputs are restricted to
simple repository-relative strings.

### Single-store local DAG

Set `ci.backend = "nix-local";` to run all exact-head Nix checks inside
one Nix store. This avoids cross-runner duplicate derivation builds without
Tailscale, remote workers or shipping artifacts between runners. By default
it runs only for pull requests, with no push or manual dispatch; callers can
explicitly opt into other events. The reusable job restores a trusted-main
cache read-only and **does not publish** new cache entries. Its tradeoff is
one runner's build capacity and slower cold builds compared with a warm
distributed pool. For small workspaces and untrusted PRs, benchmark it
against `matrix` before choosing the required check.

The paired `mkFlakeCiWorkflows` generator accepts `nix-local` as the
untrusted pull-request backend while keeping native remote builders on
trusted branches.

## Existing Nix tools

See [Nix ecosystem integrations](nix-ecosystem.md) for flake-parts, treefmt-nix, git-hooks.nix, nix-github-actions, nix-fast-build, workflow generators, and binary caches. Phenix CI composes their flake outputs rather than replacing those libraries.

## Verification and limits

Run `nix eval --json .#lib.tests` in this library to check backend dispatch,
pins, secret selection, remote trust policy and compatibility with existing
check configurations. Consumer CI must still demonstrate same-head coverage,
successful SSH store access, cold/warm runtimes and a pass from the existing
shipping evidence gate before promoting the remote backend to a required
check.

This wrapper does not provision Tailscale policy, create GitHub secrets,
install Nix locally or guarantee faster builds. Nix cache configuration
remains in the existing `mkFlakeCheckConfig.cache` and backend workflows;
the wrapper does not introduce another cache implementation.

## Matrix backend: build reuse evidence

The generic `matrix` backend verifies every evaluated Nix check at the exact
consumer commit. It now also preserves the direct `nix build -L` output from
each shard and, **after successful evidence verification**, writes a
`flake-duplicate-builds` artifact and a job-summary table. These count the
same Nix derivation being built more than once across isolated runner stores.
A fully cached run can correctly report zero local builds.

These counts **do not** establish that Rust compilations are shared when
consumers define distinct Nix derivations that each invoke `cargo test`.
For those, expose shared precompiled Cargo test packages through
`mkCargoTestPackages`, list the compiled test corpus in
`prebuildTargets`, and run the already-built binaries in each shard.
Alternatively, use the single-store native DAG or remote-builder backend to
avoid cross-runner Nix derivation duplication. A single test corpus trades
fewer compiler invocations for coarser invalidation when workspace sources
change; profile this tradeoff before splitting the corpus.
