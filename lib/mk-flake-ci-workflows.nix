# User-facing paired CI policy. One Nix declaration generates both trust
# domains; neither workflow defines another check inventory or scheduler.
{
  ci,
  configPath,
  libraryRef,
  name ? "Flake CI",
  names ? { },
}:
let
  mk = import ./mk-flake-ci-workflow.nix;
  isAttrs = builtins.isAttrs;
  keysWithin = attrs: allowed: builtins.all (key: builtins.elem key allowed) (builtins.attrNames attrs);
  fail = message: throw "phenix-flake-ci: ${message}";
  trustedName = names.trusted or "${name} (trusted Nix)";
  prName = names.pullRequest or "${name} (pull request)";
  trusted = ci.trusted or { };
  pr = ci.pullRequest or { };
  repository = trusted.repository or null;
  branches = trusted.branches or [ ];
  workflowDispatch = trusted.workflowDispatch or false;
  remote = trusted.remote or { };
  prBackend = pr.backend or "nix-dag";
  valid =
    if !isAttrs names || !keysWithin names [ "trusted" "pullRequest" ]
      || !builtins.isString trustedName || trustedName == ""
      || !builtins.isString prName || prName == "" then
      fail "names may only override nonempty trusted and pullRequest names"
    else if !isAttrs ci || !keysWithin ci [ "trusted" "pullRequest" ] then
      fail "CI policy must define only trusted and pullRequest"
    else if !isAttrs trusted || !keysWithin trusted
      [ "repository" "branches" "backend" "remote" "workflowDispatch" ] then
      fail "trusted CI accepts repository, branches, backend, remote and workflowDispatch"
    else if !isAttrs pr || !keysWithin pr [ "backend" ] then
      fail "pullRequest CI accepts only backend"
    else if (trusted.backend or "nix-remote") != "nix-remote" then
      fail "trusted workflow must use the native Nix remote scheduler"
    else if !(builtins.elem prBackend [ "nix-dag" "matrix" ]) then
      fail "untrusted pull requests must use nix-dag or matrix"
    else if !builtins.isBool workflowDispatch then
      fail "trusted.workflowDispatch must be boolean"
    else
      true;
in
assert valid;
{
  trusted = mk {
    ci = { backend = "nix-remote"; inherit remote; };
    inherit configPath libraryRef;
    name = trustedName;
    events = {
      pullRequest = false;
      pushBranches = branches;
      inherit workflowDispatch;
    };
    admission = { inherit repository branches; };
  };

  pullRequest = mk {
    ci = { backend = prBackend; };
    inherit configPath libraryRef;
    name = prName;
    events = {
      pullRequest = true;
      pushBranches = [ ];
      workflowDispatch = false;
    };
  };
}
