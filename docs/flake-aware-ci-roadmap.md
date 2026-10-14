# Flake-aware CI roadmap

Status: FC1 and FC3 candidates implemented; FC4 cache policy and prebuild evidence implemented; FC2 impact proofs pending. Consumer and release parity unverified.
Consumer: [phenix-ai](https://github.com/matthis-k/phenix-ai)
Baseline: [M1 CI matrix rollout](https://github.com/matthis-k/phenix-ai/blob/impl/m1-basic-acp/spec/m1-ci-matrix.md)

## Goal

Make `mkCi` reuse a consumer's Nix build and check definitions while producing
fast, verifiable CI jobs. Keep the consumer flake authoritative. The library
plans execution, cache policy, and evidence. It does not replace Nix evaluation,
Cargo compilation, or the consumer's conformance tests.

In Phenix AI, the 12 October same-head comparison measured 25m18s for the
serial shipping job and 11m10s for the candidate matrix. Later successful
matrix runs ranged from 9m33s to 14m03s. Parallel jobs reduced elapsed time,
but the existing fixtures still compile shared contract, SDK and proc-macro
crates dozens of times. The earlier old-main selective CI took roughly
5-6 minutes with different check coverage; it is not a full-suite benchmark.

## Responsibilities

| Owner | Responsibility |
| --- | --- |
| Consumer flake | All Nix packages and checks, fixtures, build profiles, input closures, check-specific impact hints, release evidence |
| `phenix-flake-ci` | Evaluate the check inventory, validate coverage, plan jobs, apply conservative change-impact rules, manage cache policy, emit CI evidence, generate GitHub workflow |
| GitHub Actions | Run the generated plan at an immutable commit, report outcomes, store evidence |
| Nix | Derivation identity, dependency closure, store integrity, substitution and rebuild decisions |
| Cargo | Crate dependencies and compilation inside consumer-defined derivations |

Do not add a second test registry or maintain the same check-name list in YAML.
Keep the existing `mkCi` semantic phases, `needs` DAG, Cargo impact planner,
cache ownership and generated workflow support. The new Nix integration is opt-in.

## Design contract

The consumer exposes its existing `checks.<system>` set. The planner evaluates
that set for the checked-out commit and builds a stable manifest:

- Commit SHA, system, evaluated check names and Nix derivation paths.
- Optional consumer-owned metadata for grouping, impact and required level.
- Each check's declared build dependencies and declared source inputs where
  they can be inspected.
- A complete accounting of checks selected, excluded by policy, or proven
  unaffected, with a reason for every omission.

Group hints are optional metadata. Any check lacking a group still enters the
required inventory. The final gate compares the plan against the evaluated
Nix check set. A newly added check cannot disappear because a workflow's
static list was not updated.

The optional `mkFlakeCheckConfig` now builds a versioned scheduling config.\n`lib/flake-check-plan.py` discovers check derivations, shards the full inventory,\nand verifies shard evidence. The existing semantic `mkCi` interface is unchanged.\nThis is not yet a generated workflow or a verified shipping replacement. Start with a single supported system and flake.
Do not require consumers to adopt flake-parts or a new Nix framework.

### Planning

1. Discover the exact-head check inventory through Nix evaluation.
2. Reuse the existing Cargo reverse-dependency planner for annotated Rust
   jobs. Extend it with source/derivation impact only when the planner can
   prove the mapping from changed files to Nix inputs.
3. Select required checks for the CI mode. Main, release and explicit full
   validation run the complete inventory, including shipping.
4. Group checks using consumer hints and prior measured durations. If no
   reliable measurements exist, use conservative fixed groups and preserve
   deterministic output.
5. Emit one plan digest with check names, derivation identifiers, selected
   jobs, dependencies, exclusions and reasons.
6. Make each job report its exact commit, manifest digest, selected check
   names, realized store paths and outcome. Reject missing, canceled, stale
   or mismatched results at the required gate.

Nix's derivation graph determines what may reuse built outputs. CI dependencies
express job scheduling, not a substitute for Nix dependency resolution.
Sharding must not create an extra Rust compilation merely to gain parallelism.

### Impact rules

| Change | Planning rule |
| --- | --- |
| One plugin implementation | Select affected plugin checks and reverse consumers only when derivation inputs prove the scope |
| Shared SDK, contract, ABI or macros | Select all dependent native checks, plus relevant conformance and security checks |
| Kernel implementation | Select all kernel dependents and required integration/product checks |
| Flake, lockfile, toolchain, generator or unknown file | Full relevant gate; fail open to more checks |
| Main push, release, explicit full validation | Full inventory and shipping evidence |

An unaffected decision needs a checked rule and a reproducible witness.
An unexecuted check is never reported as a pass. Keep the current Cargo
planner's conservative fallbacks and versioned manifest exceptions.

### Build reuse and profiles

`phenix-flake-ci` can schedule reusable Nix derivations, but cannot make
independently built Cargo derivations share intermediate object files.
Phenix AI must first build compatible fixture crates together, package each
native plugin separately, and narrow fixture source sets. Retain independent
compilation for external public-SDK verification, linker/LTO variants,
reproducibility checks and intentionally invalid ABI/security fixtures.

The consumer may define a fast conformance profile and a separate shipping
profile. Fast checks still execute genuine semantic tests. Release checks keep
the current optimized ABI and production-closure evidence. Compare
failure coverage before making a fast profile the default for PRs.

### Cache and security

Use Nix store paths as the reusable unit. Prefer a read-only binary cache for
PR consumers, with publication only from a trusted validated revision.
Within-run cache writers remain exclusive. GitHub PR-merge caches are not
assumed to be shared across sibling stacked PRs.

Cache keys include platform, Nix/Cargo inputs and compilation settings, but
not the exact source SHA when unchanged dependencies can be reused safely.
Let Nix check derivation identity. Treat cache misses as rebuilds, never as
validation bypasses. Never execute untrusted PR code under a
`pull_request_target` write token.

## Implementation milestones

| ID | Repository | Change | Acceptance |
| --- | --- | --- | --- |
| FC1 | phenix-flake-ci | Add opt-in exact-head Nix check discovery and a versioned plan manifest | New check appears automatically; duplicate/missing checks fail the gate; existing `mkCi` unchanged |
| FC2 | phenix-flake-ci | Add proof-based Nix input impact to existing Cargo selection | One changed fixture skips only proven unaffected checks; unknown/config/lock changes select full gate |
| FC3 | phenix-flake-ci | Add deterministic Nix check grouping, dependency-aware job DAG and one required evidence aggregate | No missing/canceled/stale shard accepted; shipping check covered; identical plan on rerun |
| FC4 | phenix-flake-ci | Add reusable cache policy and measured timing/compilation reporting | PR jobs restore trusted outputs read-only; cache reuse and elapsed/cpu time recorded |
| AI-CI1 | phenix-ai | Consolidate compatible Cargo fixture builds and narrow their Nix sources | Same-head before/after proves reduced compile count and no unrelated fixture rebuild |
| AI-CI2 | phenix-ai | Split fast semantic and optimized shipping builds where equivalence permits | Fast checks retain negative/security behavior; optimized artifact and ABI proof remains |
| AI-CI3 | phenix-ai | Adopt generated jobs, compare shipping parity, retire duplicate serial workflow | All Nix checks and exact-head release evidence match the prior required gate |
| AI-CI4 | phenix-ai | Activate proven affected-check mode after full parity | Small-change checks are skipped only with a recorded unaffected proof; main/release still full |

Dependency order: AI-CI1 can start immediately. FC1 precedes FC2/FC3.
FC4 may proceed in parallel. AI-CI2 uses AI-CI1's outputs. AI-CI3 needs FC1,
FC3, AI-CI1 and release parity. AI-CI4 needs FC2 and AI-CI3.
Keep cross-repository changes as separate PRs and retain a working fallback
until the generated gate passes on the same exact head.

## Benchmark and cutover criteria

Collect at least one cold and one warm successful run on the same commit and
runner class for serial and generated CI. Report:

- End-to-end elapsed time, per-job critical path and billed runner-minutes.
- Total Rust compilation invocations, especially contract, SDK and macros.
- Number of Nix derivations built, substituted and invalidated by one fixture edit.
- Cache hit/miss, transfer bytes, restoration time and publication scope.
- Exact-head complete check set, negative tests, shipping/closure evidence and
  aggregate outcome.

Initial targets for measurement, not promises: 2-4 minutes for a small warm
plugin PR and 5-8 minutes for warm full validation. Cold results must also be
reported. A speedup is accepted only when the equivalent check coverage and
release evidence are preserved.

## Non-goals

Do not implement a second derivation evaluator, a generic build scheduler,
kernel-specific CI rules in the library, automatic unsafe skipping, or a second
test/ABI implementation. The first version should reuse `mkCi` and the
existing generated GitHub workflow.

## Implementation record

The current branch adds `mkFlakeCheckConfig` and injected toolchain strategy
functions. `flake-check-plan.py` evaluates every check at the checked-out
commit, writes deterministic shards, computes a lockfile-derived cache identity,
prebuilds declared Nix packages and checks exact-head evidence. The reusable
GitHub workflow contains the cache writer, read-only consumers and aggregate.
Phenix AI consumes it through a thin workflow call.

This is full-suite scheduling, not selective PR skipping. FC2 and AI-CI4 still
need derivation-input impact witnesses. AI-CI1 still needs consolidation of the
duplicate Cargo fixture builds. Compare production closure and optimized
shipping output with the existing full gate before switching required checks.
