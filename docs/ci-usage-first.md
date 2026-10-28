# Generate trusted and untrusted CI from one consumer policy

Declare the CI policy in the same file as the complete flake check inventory.
The client describes **trust and capacity**, not scheduling internals.

```nix
# ci/flake-check-config.nix
let ciLib = import ../.ci-tools/flake-ci/lib/default.nix;
in ciLib.mkFlakeCheckConfig {
  system = "x86_64-linux";
  ci = {
    trusted = {
      repository = "matthis-k/phenix-ai";
      branches = [ "main" ]; # Protect these refs with GitHub rulesets.
      backend = "nix-remote";
      remote = {
        transport = "tailscale";
        credentials = "oidc";
        workers = 4;
        slotsPerWorker = 2;
      };
      # workflowDispatch = true; # Optional; ref/repository guard mandatory.
      # pullRequestAuthors = [ "matthis-k" ]; # Optional owner PR native Nix path
    };
    pullRequest.backend = "nix-dag";
  };
}
```

Render both callers from that policy:

```nix
let ciLib = import ../.ci-tools/flake-ci/lib/default.nix;
in ciLib.mkFlakeCiWorkflows {
  ci = (import ./flake-check-config.nix).ci;
  configPath = "ci/flake-check-config.nix";
  libraryRef = "0123456789abcdef0123456789abcdef01234567"; # Pinned CI library commit
  name = "Phenix CI";
}
```

The result is an attribute set with `trusted` and `pullRequest` strings.
Write both to separate `.github/workflows/*.yml` files and verify byte-for-byte
in CI. A consumer may derive `libraryRef` from the locked flake input's
`rev` instead of copying a SHA.

The trusted workflow uses **one Nix DAG scheduler** and four Tailscale
`ssh-ng` builders. It schedules and deduplicates identical derivations
through the native Nix store; it does not partition build/test work manually.
The PR workflow uses the same Nix check inventory with the existing
credential-free staged DAG backend. Neither changes the check list.

## Duplicate PR run prevention

Generated PR-capable callers include GitHub Actions workflow-level
`concurrency`: runs for the same PR and workflow name use the same group,
independent of the triggering event and head SHA. A newer PR run cancels
the older in-flight or pending run, including its remote Nix coordinator
and workers. Different PRs retain separate groups; trusted and untrusted
callers use distinct workflow names so one cannot cancel the other's jobs.
Push and manual-dispatch runs use their unique GitHub run ID, and are
not canceled by PR activity or serialised accidentally.

This is **per-PR execution deduplication**, not a global shared Nix scheduler.
Independent PRs or independently triggered jobs (format/ACP/cache tests)
can still execute overlapping work, which Nix substitutes from caches
only when compatible outputs are available.

## Trust semantics

- `trusted.repository` is mandatory and must be an exact owner/repository.
- `trusted.branches` is a nonempty exact allowlist; wildcards and duplicate
  names are rejected. This names refs the *repository owner* permits to use
  Tailscale; it is not a claim that branch names alone protect credentials.
- The generator installs a **job-level admission expression** on repository,
  event and exact ref. Manual dispatch defaults off; enabling it still
  requires the approved ref.
- Push refs in this list must be protected against untrusted edits.
  GitHub branch protection/rulesets and Tailscale federated-identity grants
  are operational responsibilities beyond YAML generation.
- By default, pull requests use the unprivileged staged backend. With
  `trusted.pullRequestAuthors = [ "matthis-k" ];`, PRs authored by the listed
  GitHub account **and whose head branch is in the trusted repository itself**
  run through the same native one-coordinator/four-builder backend as main.
  The workflow's job-level admission verifies the exact repository, PR author,
  and head-repository identity. Fork PRs and all other authors continue to use
  credential-free staged CI; the two PR jobs have complementary guards.
- The approved author must control the head branch and its allowed editors.
  Anyone able to change a trusted same-repository PR branch can run arbitrary
  build code with its delegated Tailscale builder authority. Protect this via
  repository write permissions/branch rules, not Git author or commit messages.
- The trusted GitHub pull_request event must be allowed to access the Tailscale
  federated-identity grant for the tagged builders. Branch-only OIDC grants
  will not automatically authorize a pull_request subject. Do not use
  pull_request_target to bypass these safeguards.
- Only approved same-repository PRs may select `nix-remote` and receive
  `id-token: write`; other PRs never receive Tailscale credentials or a
  trusted cache writer. Test jobs remain read-only, and remote artifact
  publication remains disabled on PR events.
- Both workflow callers use the same immutable library ref and
  `configPath`. The library's full-Nix shipping evidence remains required.

The older `mkFlakeCiWorkflow` remains a useful low-level one-workflow
renderer, while `mkFlakeCiWorkflows` is the default **usage-first** entrypoint
for repositories requiring both trusted push and untrusted PR execution.

## Automatically discovered runnable test packages

The repository may expose any number of executable packages with the flat
`test.<name>` prefix under `packages.<system>`. This is a **generic
artifact convention**, not a special CI understanding of any domain:

```nix
packages.x86_64-linux."test.smoke" = pkgs.writeShellApplication {
  name = "test-smoke";
  runtimeInputs = [ existingRuntime ];
  text = ''
    exec ${existingRuntime}/bin/example-runtime --config ${existingConfig}
  '';
};
```

The library discovers them by evaluating the flake, without maintaining a
second list, compiles their exact output closures in the same native Nix
coordinator as the existing checks, and creates **one separate job per test
package** after the coordinator. Each test job only has read permissions:
it restores the compiled store, verifies the expected output is present
without substitution, and executes `nix run --offline`. The final shipping
gate requires exact-head evidence for all discovered tests, even when one
fails. An empty test package inventory is valid.

The package implementation may use a language-specific test builder or be a
thin wrapper over an existing runtime and configuration. Build artifacts
should contain actual test programs; test execution should not recompile.
The library does not implement a domain-specific composition loader and
does not infer privileges, bindings or runtime semantics.

**Migration:** Existing `checks` remain mandatory. Some legacy check
derivations already execute tests during their Nix build; these still run
as before. New `test.*` packages are executed after build and verified
separately. Move old test work into runnable packages only once exact-head
shipping parity is demonstrated. The coordinator's GitHub Actions Nix
cache has a finite retention size; a missing test closure fails closed.
A dedicated binary cache can remove this operational limitation.

## Optional Cargo test artifacts (language-specific adapter)

The core CI scheduler remains language-agnostic. For a Rust workspace, the
opt-in helper compiles Cargo's test executables **without executing them** and
exports one ordinary `test.*` package:

```nix
packages.${system}."test.cargo-workspace" = flakeCi.mkCargoTestPackage {
  inherit pkgs;
  src = pkgs.lib.cleanSource ./.;
  lockFile = ./Cargo.lock;
  # cargoArgs = [ "--workspace" "--all-targets" ]; # defaults
};
```

The native coordinator builds that derivation only once. Afterward, the
automatically generated test job executes the precompiled test binaries
against the *immutable source-store tree* using the already-restored Nix
closure; it never runs Cargo. This preserves Rust tests that use
`env!("CARGO_MANIFEST_DIR")` to locate fixture files.

The helper is opt-in while migrating existing `checks`. It is deliberately
one build artifact and one runnable test package per declared Cargo test
group, so consumers can split workspaces into targeted source/dependency
groups without requiring the generic CI library to parse their domain model.

Do not switch existing test checks off until both paths have passed on the
same exact source revision. A whole-workspace Cargo test group will still be
invalidated by any change to its selected source tree, even if most crates
are unchanged; consumers wanting narrower incremental builds should
declare narrower groups.
