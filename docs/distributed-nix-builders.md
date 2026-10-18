# Distributed Nix builders (experimental)

Generic opt-in backend for the existing flake check DAG and exact-head evidence.
One Nix process schedules all ready derivations across GitHub-hosted ssh-ng
builders. No separate check registry or custom derivation scheduler.

## One-time Tailscale setup

1. Create a Tailscale federated identity client authorized for the consuming
   GitHub repository/workflow, with writable auth_keys scope and tag:phenix-ci.
   See https://tailscale.com/docs/integrations/github/github-action .
2. Preferred: add GitHub Actions repository secrets TS_OAUTH_CLIENT_ID and
   TS_AUDIENCE containing the federated client ID and audience (no SSH keys).
   Simpler alternative: add just TS_AUTHKEY, containing a reusable, ephemeral,
   pre-approved Tailscale auth key scoped to tag:phenix-ci. The reusable
   workflow supports either method, but rejects ambiguous mixed credentials.
   OIDC is preferred for recurring CI because it avoids stored bearer keys.
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
    secrets:
      TS_AUTHKEY: ${{ secrets.TS_AUTHKEY }} # OR the OIDC pair below
      # TS_OAUTH_CLIENT_ID: ${{ secrets.TS_OAUTH_CLIENT_ID }}
      # TS_AUDIENCE: ${{ secrets.TS_AUDIENCE }}
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
Missing credentials cause an explicit preflight error, not a fake benchmark. Do not commit the actual TS_AUTHKEY to a YAML file or expose it in logs.

The automated Python test suite validates host naming, matrix enumeration,
SSH-store discovery and missing-worker failure. It cannot validate live
tailnet authentication, Nix remote-build permissions, or cross-runner speed.

## Optional shared binary cache publication

The remote coordinator can publish the complete verified check output closures
to an existing writable Nix store instead of relying only on the ephemeral
GitHub Actions cache archive. This is **opt-in**: configure the repository
variable `NIX_BINARY_CACHE_DESTINATION` with a writable Nix store URI,
such as `s3://bucket/path` or an authorized `ssh-ng://builder` store.
The runner must already have the corresponding credentials and Nix store
trust configuration. Never put a credential in the repository variable.

After the exact-head DAG verifier passes, the coordinator invokes
`flake-check-dag.py publish`. This calls native `nix copy --to` for the
verified store paths, recursively publishing all referenced build closures
(kernel, plugins, agents, native libraries) with Nix's own identity semantics.
Only trusted push/manual execution may publish. Pull requests never write.
The existing store cache action remains a fallback; an unset destination
changes nothing. Cache consumers must configure an authorized read-only
substituter and its signing public key separately.

The output publisher does not discover extra package lists or maintain a
second build graph. If a consumer wants additional package artifacts in the
published closure, it must include them through authoritative Nix checks.
Test the configured destination and substitution on real workers before
claiming cross-run or cross-machine cache savings.

## Benchmark

Run 2, 4 and 8 workers sequentially on the same immutable consumer commit
to avoid competing with other benchmark variants. Compare elapsed workflow
time, total runner-minutes, actual Nix build time, compilation event counts,
duplicate Nix derivations and transfer overhead. The prior same-commit
staged DAG baseline is 16m45s, conventional full matrix 18m52s.
Do not claim speedup until new runs succeed and equivalent evidence is checked.
