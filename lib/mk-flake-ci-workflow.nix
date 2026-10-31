# Render a thin, pinned GitHub Actions adapter for an existing flake check plan.
# Nix still owns the check inventory. Each backend keeps its own evidence gate.
{
  ci,
  configPath,
  libraryRef,
  name ? "Flake CI",
  events ? { },
  admission ? null,
  untrustedExclusion ? null,
}:
let
  fail = message: throw "phenix-flake-ci: ${message}";
  isAttrs = builtins.isAttrs;
  keysWithin = attrs: allowed: builtins.all (key: builtins.elem key allowed) (builtins.attrNames attrs);
  safePath = path:
    builtins.isString path
    && builtins.match "[A-Za-z0-9_./-]+" path != null
    && !(builtins.elem ".." (builtins.split "/" path))
    && builtins.substring 0 1 path != "/";
  backend = ci.backend or "matrix";
  remote = ci.remote or { };
  transport = remote.transport or "tailscale";
  workers = remote.workers or 4;
  slotsPerWorker = remote.slotsPerWorker or 2;
  credentials = remote.credentials or "oidc";
  ghcrCache = remote.ghcrCache or false;
  pullRequest = events.pullRequest or (backend != "nix-remote");
  pushBranches = events.pushBranches or [ "main" ];
  workflowDispatch = events.workflowDispatch or true;
  remoteSelected = backend == "nix-remote";
  # Optional typed admission guard for a generated trusted push workflow.
  # This checks manual dispatch refs too; raw GitHub expressions are never
  # accepted from the consumer.
  safeRepository = repository:
    builtins.isString repository
    && builtins.match "[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+" repository != null;
  safeBranch = branch:
    builtins.isString branch
    && builtins.match "[A-Za-z0-9_]+([./-][A-Za-z0-9_]+)*" branch != null;
  admissionBranches = if admission == null then [ ] else admission.branches or [ ];
  admissionAuthors = if admission == null then [ ] else admission.pullRequestAuthors or [ ];
  exclusionAuthors = if untrustedExclusion == null then [ ] else untrustedExclusion.authors or [ ];
  safeAuthor = author:
    builtins.isString author
    && builtins.match "[A-Za-z0-9]+(-[A-Za-z0-9]+)*" author != null
    && builtins.stringLength author <= 39;
  unique = authors:
    builtins.length authors == builtins.length (builtins.attrNames (
      builtins.listToAttrs (map (author: { name = author; value = true; }) authors)
    ));
  # Both callers share this exact identity expression. A fork never qualifies:
  # author identity alone does not grant access to trusted Nix workers.
  prIdentity = repository: authors:
    "github.repository == '${repository}'"
    + " && github.event.pull_request.head.repo.full_name == '${repository}'"
    + " && (" + builtins.concatStringsSep " || " (
      map (author: "github.event.pull_request.user.login == '${author}'") authors
    ) + ")";
  admissionLines =
    if admission == null then [ ] else
    let
      branches = builtins.concatStringsSep " || " (
        map (branch: "github.ref == 'refs/heads/${branch}'") admissionBranches
      );
      pushPredicate =
        "(github.event_name == 'push' || github.event_name == 'workflow_dispatch')"
        + " && (${branches})";
      predicate =
        if admissionAuthors == [ ] then
          "github.repository == '${admission.repository}' && " + pushPredicate
        else
          "github.repository == '${admission.repository}'"
          + " && ((" + pushPredicate + ")"
          + " || (github.event_name == 'pull_request' && ("
          + prIdentity admission.repository admissionAuthors + ")))";
    in
    [ ("    if: " + "$" + "{{ " + predicate + " }}") ];
  exclusionLines =
    if untrustedExclusion == null || exclusionAuthors == [ ] then [ ] else
    [ ("    if: " + "$" + "{{ github.event_name == 'pull_request' && !("
      + prIdentity untrustedExclusion.repository exclusionAuthors + ") }}") ];
  workflowFile = {
    matrix = "flake-checks.yml";
    nix-dag = "flake-dag-staged.yml";
    nix-remote = "flake-nix-builders.yml";
  }.${backend} or (fail "unsupported CI backend: ${backend}");
  valid =
    if !isAttrs ci || !keysWithin ci [ "backend" "remote" ] then
      fail "ci must contain only backend and remote"
    else if !builtins.isString backend || !(builtins.elem backend [ "matrix" "nix-dag" "nix-remote" ]) then
      fail "backend must be matrix, nix-dag or nix-remote"
    else if !isAttrs remote || !keysWithin remote [ "transport" "workers" "slotsPerWorker" "credentials" "ghcrCache" ] then
      fail "remote must contain only transport, workers, slotsPerWorker, credentials and ghcrCache"
    else if !remoteSelected && remote != { } then
      fail "remote options require backend = nix-remote"
    else if !builtins.isBool ghcrCache then
      fail "remote.ghcrCache must be boolean"
    else if remoteSelected && transport != "tailscale" then
      fail "only the Tailscale remote transport is supported"
    else if remoteSelected && (!builtins.isInt workers || workers < 1 || workers > 16) then
      fail "remote workers must be between 1 and 16"
    else if remoteSelected && (!builtins.isInt slotsPerWorker || slotsPerWorker < 1 || slotsPerWorker > 32) then
      fail "remote slotsPerWorker must be between 1 and 32"
    else if remoteSelected && !(builtins.elem credentials [ "oidc" "authkey" ]) then
      fail "remote credentials must be oidc or authkey"
    else if !safePath configPath then
      fail "configPath must be a safe repository-relative path"
    else if !builtins.isString libraryRef || builtins.match "[0-9a-f]{40}" libraryRef == null then
      fail "libraryRef must be an exact 40-character Git commit SHA"
    else if !builtins.isString name || name == "" then
      fail "workflow name must be non-empty"
    else if !isAttrs events || !keysWithin events [ "pullRequest" "pushBranches" "workflowDispatch" ] then
      fail "unsupported workflow events"
    else if !builtins.isBool pullRequest || !builtins.isBool workflowDispatch then
      fail "pullRequest and workflowDispatch must be booleans"
    else if !builtins.isList pushBranches || !(builtins.all safePath pushBranches) then
      fail "pushBranches must be repository branch names"
    else if !(pullRequest || workflowDispatch || pushBranches != [ ]) then
      fail "at least one workflow event must be enabled"
    else if remoteSelected && pullRequest && admissionAuthors == [ ] then
      fail "remote pull_request requires an explicit approved-author admission"
    else if admission != null && (!remoteSelected || !isAttrs admission
      || !keysWithin admission [ "repository" "branches" "pullRequestAuthors" ]) then
      fail "typed admission is only supported for trusted remote CI"
    else if admission != null && (!builtins.isList admissionAuthors
      || !(builtins.all safeAuthor admissionAuthors)
      || !unique admissionAuthors
      || (admissionAuthors != [ ] && !pullRequest)) then
      fail "trusted PR author allowlist must contain unique GitHub logins and a PR trigger"
    else if untrustedExclusion != null && (remoteSelected || !pullRequest
      || !isAttrs untrustedExclusion
      || !keysWithin untrustedExclusion [ "repository" "authors" ]
      || !safeRepository (untrustedExclusion.repository or null)
      || !builtins.isList exclusionAuthors
      || !(builtins.all safeAuthor exclusionAuthors)
      || !unique exclusionAuthors) then
      fail "PR exclusion requires a valid typed repository and author allowlist"
    else if admission != null && !safeRepository (admission.repository or null) then
      fail "admission.repository must be an exact GitHub owner/repo"
    else if admission != null && (!builtins.isList admissionBranches
      || admissionBranches == [ ] || !(builtins.all safeBranch admissionBranches)
      || admissionBranches != pushBranches
      || builtins.length admissionBranches != builtins.length (
        builtins.attrNames (builtins.listToAttrs (
          map (branch: { name = branch; value = true; }) admissionBranches
        ))
      )) then
      fail "admission requires exact, nonempty branches matching pushBranches"
    else
      true;
  eventLines =
    (if pullRequest then [ "  pull_request:" ] else [ ])
    ++ (if pushBranches != [ ] then
      [ "  push:" "    branches:" ]
      ++ map (branch: "      - ${builtins.toJSON branch}") pushBranches
    else [ ])
    ++ (if workflowDispatch then [ "  workflow_dispatch:" ] else [ ]);
  inputs =
    [
      "      config: ${builtins.toJSON configPath}"
      "      library_ref: ${builtins.toJSON libraryRef}"
    ]
    ++ (if remoteSelected then [
      "      worker_count: ${toString workers}"
      "      slots_per_worker: ${toString slotsPerWorker}"
    ] else [ ])
    ++ (if remoteSelected && ghcrCache then [
      "      ghcr_cache: true"
    ] else [ ]);
  secret = key: "      ${key}: " + "$" + "{{ secrets." + key + " }}";
  remoteSecrets =
    if !remoteSelected then [ ]
    else if credentials == "oidc" then [
      "    secrets:"
      # Support client IDs stored as Actions secrets or variables. The
      # Tailscale OIDC audience is derived from the selected client ID.
      ("      TS_OAUTH_CLIENT_ID: " + "$" + "{{ secrets.TS_OAUTH_CLIENT_ID || vars.TS_OAUTH_CLIENT_ID }}")
      ("      TS_AUDIENCE: " + "$" + "{{ format('api.tailscale.com/{0}', secrets.TS_OAUTH_CLIENT_ID || vars.TS_OAUTH_CLIENT_ID) }}")
    ] else [
      "    secrets:"
      (secret "TS_AUTHKEY")
    ];
  # All PR events for one PR share a workflow-level cancellation key,
  # regardless of which commit or base branch triggered the rerun. A run
  # replaces an older in-flight run rather than launching another four-worker
  # cluster. Push/dispatch events use their unique run_id so protected-branch
  # builds are never implicitly serialized or canceled.
  concurrencyLines =
    if !pullRequest then [ ] else [
      ""
      "concurrency:"
      ("  group: " + "$" + "{{ github.workflow }}-" + "$" + "{{ github.event_name == 'pull_request' && format('pr-{0}', github.event.pull_request.number) || github.run_id }}")
      ("  cancel-in-progress: " + "$" + "{{ github.event_name == 'pull_request' }}")
    ];
  lines =
    [ "name: ${builtins.toJSON name}" "on:" ]
    ++ eventLines
    ++ concurrencyLines
    ++ [ "" "permissions:" "  contents: read" ]
    ++ (if remoteSelected then [ "  id-token: write" ] else [ ])
    ++ (if remoteSelected && ghcrCache then [ "  packages: write" ] else [ ])
    ++ [
      ""
      "jobs:"
      "  flake-checks:"
    ]
    ++ admissionLines
    ++ exclusionLines
    ++ [
      "    uses: matthis-k/phenix-flake-ci/.github/workflows/${workflowFile}@${libraryRef}"
      "    with:"
    ]
    ++ inputs
    ++ remoteSecrets;
in
assert valid;
builtins.concatStringsSep "\n" lines + "\n"
