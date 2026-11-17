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
share the same assertion language. There is no second registration step. Both `mkSuite` and `mkTestCases` now
reuse a pure defaults/specification normalizer. Shared process setup can
therefore be authored once without adding runtime state or a test scheduler.

`mkCargoTestPackages` is a compilation adapter for Cargo, not a distinct
scheduling system: workspace test binaries compile as shared artifacts and
ordinary `test.*` package wrappers select them for runtime execution.

## Runtime environments

Implemented: shell-free commands, ordered steps, declared text fixtures,
step references, local managed processes, allocated Unix sockets, socket
creation readiness, process-group termination, bounded test-step stdout and
stderr capture (1 MiB per stream by default), bounded service-log files
(1 MiB per stream) with independent recent 4 KiB tails, failure evidence
and fresh temporary working directories.

All command and fixture references are validated before launching a managed
service. Every readiness socket has exactly one declared service owner;
duplicate readiness claims fail preflight rather than allowing a false
positive based on another service's socket file. Content inspection of generated files is bounded to 16 MiB; size-only
checks remain available for larger artifacts. After failed startup, earlier services are rolled back. After a
test, service groups and command-step process groups are terminated, including
children of exited parents and timed-out commands.

Do **not** infer transport or application health from socket existence:
readiness currently proves only that a Unix socket file exists. The client
step must assert its actual handshake or response. There is currently no
automatic TCP-port allocation, VM/container backend, remote-host placement,
service-graph dependency resolution or network namespace isolation.

## Build and run boundary

1. Evaluate the exact commit's `checks` and `packages.test.*` inventories.
2. Present check and runtime-package roots together to **one Nix invocation**
   at each build boundary: the trusted coordinator builds all roots in one
   graph; the staged untrusted join combines dependent checks and runtime
   packages after importing ready-leaf outputs. Both paths share the same
   realization, output matching and offline pinning helpers. Staged workers
   still use separate stores, so earlier-stage cross-worker deduplication
   remains less complete than the single-store trusted scheduler.
3. Verify realized output paths against the frozen plan, bind the staged join
   evidence to the runtime-plan digest, and transfer their
   runtime closures to independent tester jobs.
4. Resolve each `test.*` executable once at planning time using Nix's
   `meta.mainProgram`, then `pname`, then derivation name. Freeze its store
   path (`exe`) alongside the derivation and output in a schema-2 manifest.
   On testers, import the closure, verify its output offline, and execute the
   exact frozen executable directly. No flake evaluation or Nix build occurs.
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
  first, then schedule deterministic **bounded batches** (default: at most
  32 GitHub jobs and approximately four tests per job before reaching that
  cap). Each case runs in its own test-runner workspace, with a separate
  evidence file. A batch imports the closure just once, continues after
  individual case failures and fails as a job if any case fails. The acceptance
  gate verifies the complete exact-head test inventory; more than 256 cases
  no longer silently disappear or exceed the GitHub matrix limit.
- **Runner isolation versus performance:** unlike one job per case, cases
  in one batch share a GitHub runner and Nix store. The temporary test
  workspace is not a security sandbox; a malicious case can affect subsequent
  cases in its batch. Use explicit independent job placement for tests that
  require host-level isolation. Defaults are adjustable during planning via
  `--max-test-jobs` (1 to 256) and `--tests-per-job` (positive integer).
  Duration-aware balancing requires measured case timings and is not claimed.
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
   fails explicitly; service files are capped and recent tails preserved
   independently. Introduce configurable service-health policies only with
   documented behavior for legitimate long-running or one-shot services.
4. **Overhead optimization:** measure per-test startup and artifact transport
   before adding optional batching; keep independent failure reporting when
   tests share a runner.
5. **End-to-end parity:** compare exact-head check inventory, test runtime
   results, release/shipping proofs, critical path, runner-minutes and
   cold/warm Nix reuse before retiring old workflows.

No claim of consumer cutover, improved wall-clock time or full backend coverage
follows from passing the library's unit and integration tests alone.

## Defaults normalization decision

**Problem:** `mkTestCases` supported shared command, environment and fixture
defaults, while `mkSuite` repeated those declarations for each runtime test.
Copying merge behavior into `mkSuite` would create another evolving ruleset.

**Baseline:** leave declarations verbose; no additional code, but repeated
service/socket setup across integration cases and divergent merge semantics.

**Selected:** one private, pure `normalize-test-specs.nix` shared by both
public constructors. Only `mkTest` compiles specifications into artifacts;
the existing native/staged workflows still schedule them. No external API
migration or second registry is introduced.

**Tradeoff:** an extra internal helper file; compensated by removing duplicated
default-merging code and supplying common service/socket defaults without a
special runtime adapter. Shallow service-attribute overrides are intentional:
users override an entire named service definition, not arbitrary nested
fields. Assertions are always per test, not inherited from another test.

**Verification:** Nix API evaluation proves shared command/env/fixtures,
ordered socket deduplication, service merging, and build/runtime projection.
Consumer integration and equal CI behavior still require separate parity
evidence.

## Runtime credential boundary

Runtime test jobs must not inject `GITHUB_TOKEN` into Nix's
`access-tokens` setting. The install action can add that token by default,
so both backends explicitly configure an empty `access-tokens` value on
test-only jobs, and neither passes the token as an installer input. Test jobs
have no repository checkout at all: the plan artifact carries a copy of the
pinned library's execution script beside the manifest, and GitHub's exact
source SHA supplies manifest authority. They retain read-only `contents`
permissions. This reduces exposed authority but does not
isolate arbitrary test executables from the host or guarantee network
confinement. Higher-trust secrets must stay out of test jobs.

Service startup also rolls back partially started processes when interrupted,
including cancellation signals handled as Python exceptions, rather than only
on ordinary startup failures.

## Fresh runner closure proof

Library CI exports the exact-head manifest, complete runtime closure and
runner script as one artifact. A **separate GitHub Actions runner with no
source checkout** installs Nix without API access tokens, imports the closure,
executes the frozen binaries from a temporary directory without `flake.nix`,
and verifies both test outcomes against the original manifest. The GitHub run
commit SHA supplies exact-head authority without requiring a local Git tree.

The executable name follows Nix's documented derivation application order:
`meta.mainProgram`, `pname`, then the parsed derivation name. Invalid paths,
missing binaries and non-executable package outputs fail instead of attempting
a rebuild. The inventory schema is intentionally replaced with version 2,
without a compatibility shim for the older manifest format.

## Batched tester evidence

`flake-test-packages.py plan` generates a deterministic matrix with entries
`{ id, names }` directly from the frozen `test.*` inventory. Both trusted
and staged runtime backends feed `names` to the same `batch` command. The
command imports the immutable NAR closure once, checks the selected output
store paths offline, runs each frozen executable and writes
`<results>/<name>/evidence.json`. Failures are collected without skipping
other cases, and the final acceptance gate rejects missing, repeated, stale,
or failed case evidence. The existing single-case `run` command remains
available for jobs requiring independent runner isolation, not as a legacy shim.

## Literal check names across Nix backends

Check inventory names are literal attributes under `checks.<system>`, not
fragments of an attribute path. Every generic Nix backend uses the same
quoted installable formatter so names like `api.integration+literal` resolve
to one derivation, just like already quoted `test.*` runtime packages.

## Staged DAG failure recovery

On a successful untrusted staged DAG join, no recovery job is scheduled. If
that join fails, the fallback submits **all planned Nix check roots and runnable
test package roots together** to the existing native DAG builder with Nix
`--keep-going`. This also applies to projects without executable runtime
tests, so a failed fan-out shard cannot suppress diagnostics from otherwise
independent checks. Where runtime packages were successfully realized, the
fallback exports just those packages and runs the existing artifact-only test
batches. Missing packages are recorded as blocked; the final acceptance job
still fails on the original broken join, regardless of fallback success.
If none of the packages built successfully, the recovery coordinator itself
writes one strict, exact-head blocked evidence record per test and uploads
these for the normal final verifier. It does not start empty GitHub test
runners or invent passing results. Partial availability still uses the
existing artifact-only runtime matrix, which continues independent cases.
Before the failed-only fallback build, completed leaf archives from the same
GitHub run are imported when their exact-head shard evidence and offline Nix
outputs validate. Missing, stale or invalid leaf artifacts are skipped one
by one, without preventing independent roots from being retried. This can
avoid recompilation of successful leaf outputs. It incurs no additional
compilation or artifact transfer on the successful path.

## Per-case workspace discipline

Every direct runtime executable, including a generic third-party package,
starts from a fresh temporary working directory with independent `HOME`,
`TMPDIR` and XDG user cache/config/data paths. `PHENIX_TEST_OUTPUT` remains
a stable evidence directory outside the temporary workspace. This prevents
accidental relative-file contamination between sequential cases in a batch
without misrepresenting the process-level isolation as a container or security
sandbox. Tests depending on checkout-relative files must package those files
into their Nix closure, rather than read the source tree from a test job.

## Bounded runtime report ingestion

Generated `runtime-result/result.json` is limited to 4 MiB before JSON
parsing and must be a regular file, not a symlink. An oversized or invalid
report fails the individual test and is recorded in its small, independent
evidence file, rather than permitting unbounded memory use when several
tests share a runner. This is distinct from the 16 MiB content-assertion
limit and the 1 MiB process-stream capture limit.

## Generic runtime test duration

Executable test packages use the existing `test_services.run_isolated`
process-group executor shared with declarative test steps. It imposes a
**300-second default outer timeout per case**, kills its process group on
completion/timeout, and bounds stdout and stderr to 1 MiB per stream.
Failures retain bounded 4-KiB output tails in per-case evidence while
later tests in the batch continue. The CI plan artifact carries this
shared helper beside the pinned runtime runner, avoiding any new
subprocess-lifecycle implementation. The typed policy
`ci.runtimeTests.timeoutSeconds` allows 1–3600 seconds. Job-level
timeouts remain independent, and process-group isolation does not
constrain intentionally detached children as containers would.

## Measured runtime-closure transfer optimization

Both reusable CI backends upload the immutable NAR handoff with GitHub
artifact compression level 1. The existing library fixture yielded
219,038,166 bytes at level 0 versus 101,208,854 bytes at level 1, a 53.8%
reduction. A real Phenix AI consumer pilot produced a 221 MB uncompressed
closure before the backend change. Those are *transfer-size measurements*;
they do not prove a particular overall CI speedup. The compression level is
kept fixed and fast rather than becoming another public setting.

## Runtime tester Nix store reuse

Trusted coordinator jobs own the persistent Nix store cache. Runtime test jobs
already download and import a complete same-run NAR closure for all planned
test outputs, so the extra read-only Nix cache restoration before this import
was redundant. The test stage now imports its NAR without restoring the
coordinator cache, avoiding duplicate transfer and cache extraction.
The exact-head gate is unchanged. Actual time savings depend on workload and
are not inferred from a library-only test.

## Build-only runner compatibility boundary

The older `flake-checks.yml` matrix and standalone
`flake-local-dag.yml` runners only implement `checks.*` compilation.
They have no runtime `test.*` handoff or execution gate. To avoid a
false-green CI result, they now use the **same** flake test-package discovery
helper at planning time and **reject** any nonempty runtime inventory before
dispatching builds. Consumers with runtime tests use the fully supported
`nix-dag` or `nix-remote` backend instead. This is a deliberate
fail-closed boundary, not a parallel test implementation.

## Builder job minimalism

The remote builder performs only three domain-neutral actions: install Nix,
join the tailnet, and wait for the coordinating Nix scheduler to build
derivations through ssh-ng. Previously every builder also checked out the
entire CI library to run its `hostname` and `hold` helper. The existing
exact-head plan artifact now carries a copy of the pinned worker script, so
those four redundant repository checkouts are removed. The same pinned script
and verified artifact handoff already serve runtime tester jobs. This does
not alter Nix scheduling, Tailscale admission, worker privileges or output
verification; its benefit is simpler provisioning and reduced setup I/O.

## OIDC permission scope

Only the two jobs that actually join Tailscale—remote builders and the Nix
coordinator—request `id-token: write`. Planning, isolated runtime testing
and final evidence verification retain `contents: read` and cannot request
an OIDC token. GitHub's job-scoped permissions provide this boundary directly,
without a separate credential broker. Worker jobs still execute only on the
trusted workflow, never the untrusted PR fallback.

## Pull-request cancellation identity

The untrusted staged reusable workflow uses the **PR number**, not the
head branch name, as its cancellation key. Two unrelated PRs can use the
same branch name (especially across forks), but must never cancel each
other's test runs. Push-triggered invocations do not cancel each other,
matching the generated caller workflow policy. This requires no new
scheduler or worker state.

## Exhaustive build failure behavior

All multi-root Nix compile invocations use `--keep-going` with one native Nix graph.
A failing derivation does not interrupt independent derivations; only dependents
of the failed derivation cannot be built. The build step exits nonzero **after**
Nix finishes its best-effort traversal. We do not turn errors into successful
check evidence, publish failed outputs, or rebuild package roots one by one.

GitHub matrix runners use `fail-fast: false`. A failed matrix job can be shown
as failed while independent matrix jobs continue. A **single** Nix coordinator
job cannot finish red before its own Nix invocation completes. Adding an
independent reporter job for earlier red status is not currently justified by
its extra credentials/coordination. Runtime handoff no longer requires **every** check to succeed on the
trusted native-remote backend. After Nix finishes its best-effort build graph,
the coordinator probes the exact-commit runtime output store paths. It exports
only built, executable cases; those cases run on source-free, low-privilege
tester jobs even when an unrelated check failed. Unbuilt cases receive explicit
`blocked` evidence, never a fabricated pass. Test jobs still wait for the
coordinator to finish, not necessarily succeed; a failed coordinator **always**
makes the final acceptance gate fail. A missing runtime closure never triggers
recompilation on a tester. The staged untrusted backend retains the existing
all-build-success barrier until its matching handoff is implemented and tested.

The local CLI uses the same ready-output probe and per-case evidence without
a NAR export/import. It similarly continues after unrelated Nix failures and
returns nonzero. Neither backend claims that tests downstream of an actual
compilation failure can execute; they are explicitly blocked.
