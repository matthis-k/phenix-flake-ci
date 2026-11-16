# Workload-independent CI performance evaluation

Status: reporting foundation, not evidence that any backend is faster.

## Goal

Measure *the same changed workload* before and after a CI change without
assuming a Rust workspace, a fixed number of Nix checks, four remote workers,
or even GitHub matrix jobs. Phenix AI is one consumer benchmark, **not** a
proxy for every consumer of phenix-flake-ci.

The scheduler remains native Nix. Reporting must never become a second
check registry or affect the required CI gate.

## Evidence and comparison rules

1. Use exact consumer revision, exact declared check/test inventory, Nix
   toolchain, architecture and feature profiles on both sides. Compare
   separately when any of these differ. For coordinated Nix workflows, retain
   plan and graph digests and output evidence.
2. Tag cache state for each run: empty/cold store, restored/warm store, and
   repeated no-change run are distinct scenarios. Never infer cache warmth
   from a short runtime.
3. Record total workflow elapsed time, runner-minutes, per-job elapsed time,
   peak overlap, observed between-job idle gaps, initial job-start delay,
   finalization lag, Nix build time,
   compile/cache hit evidence, runtime closure bytes, and the exact pass/fail/
   skipped inventory. Job intervals by themselves cannot identify the Nix
   dependency critical path.
4. For comparisons, perform repeated paired runs of each variant in an
   alternating order with equivalent cache and worker conditions. Report
   median and dispersion, not the fastest individual run. Include setup/
   transport and total runner-minutes, not just the Nix build command.
5. Include negative runs (one unrelated compile failure, two independent
   failures, shared-dependency failure, test failure, missing runtime artifact)
   as *diagnostic coverage* evidence. Never use these as passing CI runs.
6. If the evidence does not establish inventory or environment parity, report
   descriptive timings without assigning causal speedup.

## Workload profiles

The framework should be tested across different *shapes*. A project can
contribute to more than one profile; no consumer must implement all of them.

| Profile | Example | Stress point |
| --- | --- | --- |
| Deep Cargo workspace | Phenix AI | Shared compilation versus independent package failures |
| Small Nix project | Minimal flake fixture | Coordinator, evaluation and runner startup overhead |
| Many independent checks | Synthetic Nix fixture | Parallel execution, reporting and matrix size |
| Deep dependent checks | Synthetic Nix fixture | Dependency barriers and correctly blocked downstream work |
| Mixed-language source | Nix/Python or other fixture | No Cargo-only assumptions in planning or reports |
| Runtime-heavy services | Declarative test fixture | Handoff bytes, service lifecycle and batch balance |
| Failure-rich changes | Deliberately broken fixture | Independent failure continuation and complete diagnostics |

Use native Nix derivation identity and available cache evidence to analyze
duplicate **realizations**. Two different Cargo derivations can compile the
same Rust dependencies yet never have identical .drv paths; Nix duplicate
counters alone cannot prove semantic compilation reuse.

## Reproducible GitHub job timing

Download the completed run and **all paginated** jobs for a specific commit:

```sh
gh api repos/OWNER/PROJECT/actions/runs/RUN_ID > run.json
gh api --paginate --slurp \
  'repos/OWNER/PROJECT/actions/runs/RUN_ID/jobs?per_page=100' \
  | jq '{total_count: ([.[] | .jobs[]] | length), jobs: [.[] | .jobs[]]}' \
  > jobs.json
python3 lib/critical-path-report.py --run run.json --jobs jobs.json --head HEAD_SHA
```

Alternatively, combine GitHub's paginated `jobs` arrays into
`{"total_count": N, "jobs": [...]}` without dropping an entry.
The reporter rejects incomplete inventories and wrong heads.

For intentionally failing workflow benchmarks, add `--diagnostic`. The
output explicitly retains the failed run/job conclusions; the reporter
does not make them passing evidence. Skipped jobs are listed separately
and excluded from runner-minutes, even when GitHub omits their timestamps.
The older `tail_shard` and `shards` fields appear only for workflows that
actually contain `Check shard-...` job names. All layouts produce `jobs`
and generic wall/runner-time fields.

## Phenix AI observation (2026-11-16)

Source: [Phenix AI run 43857321453](https://github.com/matthis-k/phenix-ai/actions/runs/43857321453),
exact head `3f2c7cd081a39639a68f81fa76b871df77357e87`.

- Workflow started 11:23:27Z, updated 11:25:58Z: **151 seconds**.
- Eight executed jobs: **416 runner-seconds (6.93 runner-minutes)**.
- Coordinator: 83 seconds; four builder jobs: 70, 71, 72 and 71 seconds.
- Plan: 26 seconds; runtime batch: 14 seconds; final gate: 9 seconds.
- Between-job idle gaps: **6 seconds**; peak measured overlap: **5 jobs**.
- Runtime closure artifact: **102,074,372 bytes** (approximately 97.3 MiB).
- All eight jobs passed, with four runtime/artifact evidence uploads.

This is a successful functional smoke observation, **not** a cold-versus-warm
or alternative-worker-count benchmark. The current consumer still runs
multiple distinct Cargo derivations. To test a shared-dependency strategy,
hold the exact test inventory and independent-failure semantics fixed before
claiming a speedup.

## Different-shape observation: Flake CI's own framework run

Source: [Flake CI run 43854618477](https://github.com/matthis-k/phenix-flake-ci/actions/runs/43854618477),
exact head `9d31d9317f92f3ae4d364816b6a90a29258e17be`.

- Mixed Nix/Python framework tests and small Cargo fixture, **two sequential
  GitHub jobs** rather than four Nix remote builders.
- Workflow wall: **120 seconds**; total job time: **114 seconds
  (1.90 runner-minutes)**.
- Framework evaluation job: 98 seconds; prebuilt-runtime job: 16 seconds.
- Initial job-start delay: 3 seconds; between-job gap: 2 seconds;
  finalization lag: 1 second; peak running jobs: 1.

This demonstrates why a "four-worker" job-name filter or a
`Check shard-...` requirement is not a general performance reporter.
The two runs execute different test inventories and cannot be used as
an A/B speedup comparison.

## Acceptance for an optimization

Prefer the simplest implementation that reduces median wall time and/or
runner-minutes without reducing independent failure discovery, check inventory,
local reproducibility or strict final shipping evidence. A more complicated
planner is not justified by a single fast consumer run. If one workload
improves and another regresses, report the distribution and consider an
opt-in strategy based on existing declarative capabilities rather than
hard-coding Phenix AI's dependency graph.
