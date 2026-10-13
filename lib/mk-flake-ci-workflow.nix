# Render a thin, pinned GitHub Actions adapter for an existing flake check plan.
# Nix still owns the check inventory. Each backend keeps its own evidence gate.
{
  ci,
  configPath,
  libraryRef,
  name ? "Flake CI",
  events ? { },
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
  pullRequest = events.pullRequest or (backend != "nix-remote");
  pushBranches = events.pushBranches or [ "main" ];
  workflowDispatch = events.workflowDispatch or true;
  remoteSelected = backend == "nix-remote";
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
    else if !isAttrs remote || !keysWithin remote [ "transport" "workers" "slotsPerWorker" "credentials" ] then
      fail "remote must contain only transport, workers, slotsPerWorker and credentials"
    else if !remoteSelected && remote != { } then
      fail "remote options require backend = nix-remote"
    else if remoteSelected && transport != "tailscale" then
      fail "only the Tailscale remote transport is supported"
    else if remoteSelected && (!builtins.isInt workers || workers < 1 || workers > 16) then
      fail "remote workers must be between 1 and 16"
    else if remoteSelected && (!builtins.isInt slotsPerWorker || slotsPerWorker < 1 || slotsPerWorker > 64) then
      fail "remote slotsPerWorker must be between 1 and 64"
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
    else if remoteSelected && pullRequest then
      fail "untrusted pull_request triggers are forbidden for Tailscale builders"
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
    ] else [ ]);
  secret = key: "      ${key}: " + "$" + "{{ secrets." + key + " }}";
  remoteSecrets =
    if !remoteSelected then [ ]
    else if credentials == "oidc" then [
      "    secrets:"
      (secret "TS_OAUTH_CLIENT_ID")
      (secret "TS_AUDIENCE")
    ] else [
      "    secrets:"
      (secret "TS_AUTHKEY")
    ];
  lines =
    [ "name: ${builtins.toJSON name}" "on:" ]
    ++ eventLines
    ++ [ "" "permissions:" "  contents: read" ]
    ++ (if remoteSelected then [ "  id-token: write" ] else [ ])
    ++ [
      ""
      "jobs:"
      "  flake-checks:"
      "    uses: matthis-k/phenix-flake-ci/.github/workflows/${workflowFile}@${libraryRef}"
      "    with:"
    ]
    ++ inputs
    ++ remoteSecrets;
in
assert valid;
builtins.concatStringsSep "\n" lines + "\n"
