# Usage-first contract: one policy, two secure workflows, one check inventory.
let
  mk = import ./mk-flake-ci-workflows.nix;
  sha = "0123456789abcdef0123456789abcdef01234567";
  base = {
    configPath = "ci/flake-check-config.nix";
    libraryRef = sha;
    name = "Phenix";
  };
  policy = {
    trusted = {
      repository = "matthis-k/phenix-ai";
      branches = [ "main" ];
      backend = "nix-remote";
      remote = {
        transport = "tailscale";
        credentials = "oidc";
        workers = 4;
        slotsPerWorker = 2;
      };
    };
    pullRequest.backend = "nix-dag";
  };
  make = ci: mk (base // { inherit ci; });
  values = make policy;
  flat = builtins.replaceStrings [ "\n" ] [ " " ];
  trusted = flat values.trusted;
  pr = flat values.pullRequest;
  fail = config: !(builtins.tryEval (builtins.deepSeq (make config) true)).success;
  failCall = extra: !(builtins.tryEval (builtins.deepSeq (mk (base // extra)) true)).success;
in
{
  singlePolicyEmitsBothNativeAndUntrustedWorkflows =
    assert builtins.match ".*flake-nix-builders.yml@${sha}.*" trusted != null;
    assert builtins.match ".*flake-dag-staged.yml@${sha}.*" pr != null;
    assert builtins.match ".*library_ref: \"${sha}\".*" trusted != null;
    assert builtins.match ".*library_ref: \"${sha}\".*" pr != null;
    assert builtins.match ".*config: \"ci/flake-check-config.nix\".*" trusted != null;
    assert builtins.match ".*config: \"ci/flake-check-config.nix\".*" pr != null;
    true;

  nativeDefaultsAreFourBuildersAndTwoSlots =
    assert builtins.match ".*worker_count: 4.*" (flat (make {
      trusted = { repository = "matthis-k/phenix-ai"; branches = [ "main" ]; };
    }).trusted) != null;
    assert builtins.match ".*slots_per_worker: 2.*" trusted != null;
    true;

  trustedPushIsExplicitlyGuarded =
    assert builtins.match ".*github.repository == 'matthis-k/phenix-ai'.*" trusted != null;
    assert builtins.match ".*github.ref == 'refs/heads/main'.*" trusted != null;
    assert builtins.match ".*github.event_name == 'push'.*" trusted != null;
    assert builtins.match ".*id-token: write.*" trusted != null;
    assert builtins.match ".*TS_OAUTH_CLIENT_ID.*TS_AUDIENCE.*" trusted != null;
    assert builtins.match ".*pull_request:.*" trusted == null;
    assert builtins.match ".*workflow_dispatch:.*" trusted == null;
    true;

  prNeverReceivesRemoteAuthority =
    assert builtins.match ".*pull_request:.*" pr != null;
    assert builtins.match ".*id-token: write.*" pr == null;
    assert builtins.match ".*TS_OAUTH_CLIENT_ID.*" pr == null;
    assert builtins.match ".*TS_AUTHKEY.*" pr == null;
    assert builtins.match ".*push:.*" pr == null;
    assert builtins.match ".*workflow_dispatch:.*" pr == null;
    true;

  manualDispatchIsBoundToApprovedRefs =
    let
      value = flat (make (policy // {
        trusted = policy.trusted // {
          branches = [ "main" "ci/approved" ];
          workflowDispatch = true;
        };
      })).trusted;
    in
    assert builtins.match ".*workflow_dispatch:.*" value != null;
    assert builtins.match ".*github.ref == 'refs/heads/main'.*" value != null;
    assert builtins.match ".*github.ref == 'refs/heads/ci/approved'.*" value != null;
    assert builtins.match ".*github.repository == 'matthis-k/phenix-ai'.*" value != null;
    true;

  authkeyStillUsesOnlyItsSelectedCredential =
    let
      value = flat (make (policy // {
        trusted = policy.trusted // { remote.credentials = "authkey"; };
      })).trusted;
    in
    assert builtins.match ".*TS_AUTHKEY.*" value != null;
    assert builtins.match ".*TS_OAUTH_CLIENT_ID.*" value == null;
    true;

  prMatrixIsStillCredentialFree =
    let
      value = flat (make (policy // { pullRequest.backend = "matrix"; })).pullRequest;
    in
    assert builtins.match ".*flake-checks.yml@${sha}.*" value != null;
    assert builtins.match ".*id-token: write.*" value == null;
    true;

  unknownAndUnsafePolicyShapesFailClosed =
    assert fail { trusted = policy.trusted; unknown = "unsafe"; };
    assert fail (policy // { trusted = policy.trusted // { extra = true; }; });
    assert fail (policy // { pullRequest = { backend = "nix-remote"; }; });
    assert fail (policy // { trusted = policy.trusted // { backend = "matrix"; }; });
    assert fail (policy // { trusted = policy.trusted // { remote.workers = 17; }; });
    assert fail (policy // { trusted = policy.trusted // { repository = ""; }; });
    assert fail (policy // { trusted = policy.trusted // { branches = [ ]; }; });
    assert fail (policy // { trusted = policy.trusted // { branches = [ "main" "ci/*" ]; }; });
    assert fail (policy // { trusted = policy.trusted // { branches = [ "main" "main" ]; }; });
    assert fail (policy // { trusted = policy.trusted // { workflowDispatch = "true"; }; });
    assert failCall { libraryRef = "main"; ci = policy; };
    true;
}
