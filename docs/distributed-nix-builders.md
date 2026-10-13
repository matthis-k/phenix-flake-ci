# Distributed Nix builders (experimental)

Generic opt-in backend for the existing flake check DAG and exact-head evidence.
One Nix process schedules all ready derivations across GitHub-hosted ssh-ng
builders. No separate check registry or custom derivation scheduler.

## One-time Tailscale setup

1. Create a Tailscale federated identity client authorized for the consuming
   GitHub repository/workflow, with writable auth_keys scope and tag:phenix-ci.
   See https://tailscale.com/docs/integrations/github/github-action .
2. Add GitHub Actions repository secrets TS_OAUTH_CLIENT_ID and TS_AUDIENCE
   containing the client ID and federation audience. No SSH private keys.
3. Configure the tailnet policy to allow connectivity and Tailscale SSH from
   tag:phenix-ci to tag:phenix-ci as user runner. Merge this policy fragment
   into your existing tailnet rules (do not replace existing rules blindly):

```json
{
  "tagOwners": {"tag:phenix-ci": ["autogroup:admin"]},
  "grants": [{"src": ["tag:phenix-ci"], "dst": ["tag:phenix-ci"], "ip": ["tcp:22"]}],
  "ssh": [{"action": "accept", "src": ["tag:phenix-ci"],
           "dst": ["tag:phenix-ci"], "users": ["runner"]}]
}
```

The reusable workflow activates SSH on each ephemeral worker using the
Tailscale GitHub Action with args: --ssh. Both coordinator and workers are
automatically removed from the tailnet when their jobs exit.

## Consumer

```yaml
jobs:
  build:
    uses: matthis-k/phenix-flake-ci/.github/workflows/flake-nix-builders.yml@TESTED_SHA
    with:
      library_ref: TESTED_SHA
      config: ci/flake-check-config.nix
      worker_count: 4
      slots_per_worker: 2
    secrets: inherit
```

Supported worker counts: 1–16, with one coordinator. The coordinator and
all workers begin concurrently after DAG analysis. Only coordinator-side Nix
evaluates the graph; Nix's native scheduler dispatches through ssh-ng.

The generated hostnames incorporate run ID, attempt, pool size and worker
index, so simultaneous benchmark sizes cannot reuse host identities.
The coordinator checks the remote Nix store protocol before registering any
worker and configures a root-readable SSH client configuration. On success
or failure, it attempts to release all ready workers; workers also have a
bounded timeout. The final acceptance gate checks worker status and all
check/build evidence.

## Security and limitations

Trusted tailnet infrastructure must not execute arbitrary fork pull requests.
Use protected or manually dispatched benchmarks. Tailscale policy and GitHub
secrets are administration operations and are not available to the connected
GitHub repository integration; they require a one-time operator action.
Missing credentials cause an explicit preflight error, not a fake benchmark.

The automated Python test suite validates host naming, matrix enumeration,
SSH-store discovery and missing-worker failure. It cannot validate live
tailnet authentication, Nix remote-build permissions, or cross-runner speed.

## Benchmark

Run 2, 4 and 8 workers sequentially on the same immutable consumer commit
to avoid competing with other benchmark variants. Compare elapsed workflow
time, total runner-minutes, actual Nix build time, compilation event counts,
duplicate Nix derivations and transfer overhead. The prior same-commit
staged DAG baseline is 16m45s, conventional full matrix 18m52s.
Do not claim speedup until new runs succeed and equivalent evidence is checked.
