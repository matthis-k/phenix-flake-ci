# Unified artifact and test execution architecture

Status: implementation candidate in [phenix-flake-ci #49](https://github.com/matthis-k/phenix-flake-ci/pull/49).
The following distinguishes shipped code on the branch from next-stage design.
This document is not an alternative workflow or a second test registry.

## Objective

A consumer declares **what** to build and test. The flake library owns common
Nix packaging, execution-lifecycle helpers, plan/evidence validation and GitHub
workflows. An executable may represent a unit test, integration scenario,
conformance check, service test or end-to-end workflow: the test's *purpose*
does not determine its compilation topology or runtime backend.

## Layers (bottom-up)

| Layer | Responsibility | Current implementation |
| --- | --- | --- |
| Nix | Derivation graph, deduplication, store closure, substitution | Existing Nix engine; no custom scheduler |
| Coordinator | Exact-head inventory, shared compilation, remote workers | Existing Nix DAG and trusted `ssh-ng` remote coordinator |
| Handoff | Realize test packages, transport runtime closures, reject unbuilt outputs | `flake-test-packages.py` build/export/import/run |
| Execution | Fresh working directory, steps, service lifecycle, bounded service logs | `test-runner.py`, `test_services.py` |
| Assertions | Exit/output/JSON/files/step references, negative cases, structured failures | Existing `mkTest` assertion model |
| Consumer API | Project one declaration into flake outputs | `mkTest`, `mkSuite`, `mkTestCases`, optional Cargo adapter |
| Acceptance | Verify every expected test result against exact-head inventory | Existing plan digest and result verifier |

A layer never reconstructs another layer's dependency or check inventory.

## Current public contract

`mkTest { execution = "build"; ... }` is the default when used directly.
It returns an ordinary Nix check that executes during realization. It is
appropriate for deterministic, sandbox-compatible, cacheable checks.

`mkTest { execution = "runtime"; ... }` prepares an executable without
running its assertions at build time. Expose it as
`packages.<system>."test.<name>"` to use the existing test-package discovery
and post-build execution workflow.

`mkSuite { inherit pkgs; tests = { ... }; }` projects a single named inventory:
`execution = "runtime"` is its default, producing `suite.packages`.
Entries with `execution = "build"` produce `suite.checks`. Consumers merge
those attributes into their existing flake outputs. Both execution modes
share the same assertion language. There is no second registration step.

`mkCargoTestPackages` is a compilation adapter for Cargo, not a distinct
scheduling system: workspace test binaries compile as shared artifacts and
ordinary `test.*` package wrappers select them for runtime execution.

## Runtime environments

Implemented: shell-free commands, ordered steps, declared text fixtures,
step references, local managed processes, allocated Unix sockets, socket
creation readiness, process-group termination, bounded test-step stdout and
stderr capture (1 MiB per stream by default), bounded service-log *evidence*
tails, failure evidence and fresh temporary working directories.

All command and fixture references are validated before launching a managed
service. After failed startup, earlier services are rolled back. After a
test, service groups and command-step process groups are terminated, including
children of exited parents and timed-out commands.

Do **not** infer transport or application health from socket existence:
readiness currently proves only that a Unix socket file exists. The client
step must assert its actual handshake or response. There is currently no
automatic TCP-port allocation, VM/container backend, remote-host placement,
service-graph dependency resolution or network namespace isolation.

## Build and run boundary

1. Evaluate the exact commit's `checks` and `packages.test.*` inventories.
2. On the trusted backend, present check and test-package compilation roots to
   **one native Nix scheduler**. On the staged backend, tests are currently
   prepared after the check DAG joins, using its restored store; this shares
   existing outputs but does not yet offer the same cross-root scheduling.
3. Verify realized output paths against the frozen plan, and transfer their
   runtime closures to independent tester jobs.
4. Execute test packages offline with `--no-substitute --max-jobs 0`; a test
   runner cannot quietly compile absent artifacts.
5. Verify test-name, derivation, output path, commit, plan digest and outcome
   at the acceptance gate. Missing/canceled/failed evidence fails closed.

The native trusted backend distributes *builds* to remote workers while the
Nix coordinator remains authoritative. The staged untrusted backend has a
separate trust policy, but uses the same artifact/test contract. This is
artifact scheduling, not a duplicate framework for test-specific scheduling.

## Deliberate tradeoffs

- **Execution versus cacheability:** build-time checks are cacheable; runtime
  tests execute fresh and may require host resources.
- **Shared compiler graph versus test parallelism:** deduplicate preparation
  first, fan out execution afterwards. One job per package is currently
  simple but incurs GitHub runner startup overhead for tiny tests. Above
  256 runtime packages, planning fails explicitly until verified batching
  is available rather than silently dropping tests.
- **Safety versus environment flexibility:** Unix sockets and process groups
  are a deliberately narrow first backend. They provide lifecycle guarantees
  without implying container or VM isolation.
- **Authority versus reach:** credential-free pull requests must not inherit
  Tailscale/OIDC or trusted cache publication. Independent security gates
  remain in force.

## Next changes and acceptance criteria

1. **Consumer pilot:** after the library revision is accepted, adapt one real
   Phenix AI integration scenario using the same `mkSuite` API; keep its
   existing tests and required shipping evidence enabled until parity is
   measured.
2. **Environment capabilities:** design explicit, typed runtime requirements
   only when a real test needs TCP, isolated network, VM or remote host.
   Selection must reuse the existing plan and evidence gate rather than
   creating another scheduler. Missing capability must fail clearly.
3. **Runtime correctness:** step output is now bounded and oversize output
   fails explicitly; cap service *log files* (not merely evidence tails), and
   introduce configurable service-health policies only with documented
   behavior for legitimate long-running or one-shot services.
4. **Overhead optimization:** measure per-test startup and artifact transport
   before adding optional batching; keep independent failure reporting when
   tests share a runner.
5. **End-to-end parity:** compare exact-head check inventory, test runtime
   results, release/shipping proofs, critical path, runner-minutes and
   cold/warm Nix reuse before retiring old workflows.

No claim of consumer cutover, improved wall-clock time or full backend coverage
follows from passing the library's unit and integration tests alone.
