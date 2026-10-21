# Phenix changed-code Nix critical path — observed Actions jobs

Source: GitHub Actions run and jobs APIs for the `matthis-k/phenix-ai` exact-head five-shard matrix. All three runs completed successfully. The figures below were calculated from the recorded UTC run start/update and each job's started/completed times; **not** from estimated derivation costs.

| Run / change | Exact head | Workflow wall | Runner-minutes (sum jobs) | Last-finishing shard | Longest shard |
| --- | --- | ---: | ---: | --- | ---: |
| [#1030 changed provider binding](https://github.com/matthis-k/phenix-ai/actions/runs/39460347034) | `e7c57d9c64842d9dc447f74ba461c88e5f1cf222` | 40m01s | 112.52 | 02 | 36m52s |
| [#1039 HTTP credential storage](https://github.com/matthis-k/phenix-ai/actions/runs/39564789019) | `a6395981836e1759c74790f421dfec74ee6cfd14` | 8m36s | 15.47 | 00 | 5m41s |
| [#1040 shared credential contract](https://github.com/matthis-k/phenix-ai/actions/runs/39565271059) | `73f72bb9c149e1f06c64f71486c43ba81a70411d` | 14m37s | 41.00 | 02 | 11m37s |

## What this proves

- Long changed-contract runs are **not explained by the shared Nix writer** alone: on #1030 it took 69 seconds, versus shard 02's 2,212 seconds. #1040's writer took 58 seconds, versus shard 02's 697 seconds.
- Different changes produce different cache and invalidation profiles; #1039's last shard was 00, not 02. Do not hard-code a single shard as the bottleneck.
- Runner-minutes are summed on actual job times, not workflow duration multiplied by five. The difference measures parallelism, not billed GitHub usage.
- These three runs use different changes and cache states. They are **diagnostic observations, not an A/B speedup experiment**. They do not establish causal duplicate compilation or remote-cache substitutions.

## Reproduce with offline artifacts

Fetch the GitHub REST endpoints with a read-only token into local JSON files. The helper is intentionally network-free and does not require secrets in the artifact:

```sh
python3 lib/critical-path-report.py --run run.json --jobs jobs.json \
  --head 73f72bb9c149e1f06c64f71486c43ba81a70411d
python3 lib/test-critical-path-report.py -v
```

The `jobs.json` file must contain the **complete** `jobs` array and matching `total_count`; combine paginated responses before analysis. Mismatched head, missing pages, failed jobs and incomplete workflows are rejected.

## Next optimization experiment (B2, not implemented here)

1. Compare shard-02 log-level Nix derivation realization, cache imports and cross-shard repeated builds against a baseline with the same commit and cache state.
2. Test a **targeted** shared prerequisite or scheduling change in `phenix-flake-ci` with paired exact-head runs.
3. Keep full negative/ABI/ACP/shipping acceptance, unchanged-plugin derivation paths and original five-shard coverage. Abandon experiments that regress wall time, as happened with the broad Basic prebuild in #1024.

No optimization is claimed by the reporting PR.
