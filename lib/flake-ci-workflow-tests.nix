# Pure Nix checks for backend selection, workflow pins and trust boundaries.
let
  mkWorkflow = import ./mk-flake-ci-workflow.nix;
  mkConfig = import ./mk-flake-check-config.nix;
  sha = "0123456789abcdef0123456789abcdef01234567";
  base = {
    configPath = "ci/flake-check-config.nix";
    libraryRef = sha;
  };
  render = ci: mkWorkflow (base // { inherit ci; });
  flat = builtins.replaceStrings [ "\n" ] [ " " ];
  remote = {
    backend = "nix-remote";
    remote = {
      transport = "tailscale";
      workers = 4;
      slotsPerWorker = 2;
      credentials = "oidc";
    };
  };
  matrixYaml = flat (render { backend = "matrix"; });
  dagYaml = flat (render { backend = "nix-dag"; });
  remoteYaml = flat (render remote);
  authkeyYaml = flat (render (remote // {
    remote = remote.remote // { credentials = "authkey"; };
  }));
  invalid = x: !(builtins.tryEval (builtins.deepSeq x true)).success;
  configDefault = mkConfig { system = "x86_64-linux"; };
  configRemote = mkConfig { system = "x86_64-linux"; ci = remote; };
in
{
  matrixUsesPinnedReusableWorkflow =
    assert builtins.match ".*flake-checks.yml@${sha}.*" matrixYaml != null;
    assert builtins.match ".*library_ref: \"${sha}\".*" matrixYaml != null;
    assert builtins.match ".*pull_request:.*" matrixYaml != null;
    assert builtins.match ".*id-token: write.*" matrixYaml == null;
    true;

  nativeDagUsesSameCheckPlan =
    assert builtins.match ".*flake-dag-staged.yml@${sha}.*" dagYaml != null;
    assert builtins.match ".*config: \"ci/flake-check-config.nix\".*" dagYaml != null;
    true;

  nativePrCancellationIsScopedAndMainRunsRemainIndependent =
    let
      owner = flat (mkWorkflow (base // {
        ci = remote;
        events = { pullRequest = true; pushBranches = [ "main" ]; workflowDispatch = false; };
        admission = {
          repository = "matthis-k/phenix-ai";
          branches = [ "main" ];
          pullRequestAuthors = [ "matthis-k" ];
        };
      }));
    in
    assert builtins.match ".*concurrency:.*" owner != null;
    assert builtins.match ".*github.workflow.*github.event.pull_request.number.*github.run_id.*" owner != null;
    assert builtins.match ".*cancel-in-progress:.*github.event_name == 'pull_request'.*" owner != null;
    assert builtins.match ".*concurrency:.*" remoteYaml == null;
    true;

  remoteBuildersRequireTrustedTriggersAndOidc =
    assert builtins.match ".*flake-nix-builders.yml@${sha}.*" remoteYaml != null;
    assert builtins.match ".*worker_count: 4.*" remoteYaml != null;
    assert builtins.match ".*slots_per_worker: 2.*" remoteYaml != null;
    assert builtins.match ".*id-token: write.*" remoteYaml != null;
    assert builtins.match ".*TS_OAUTH_CLIENT_ID.*TS_AUDIENCE.*" remoteYaml != null;
    assert builtins.match ".*vars.TS_OAUTH_CLIENT_ID.*" remoteYaml != null;
    assert builtins.match ".*format.*api.tailscale.com.*" remoteYaml != null;
    assert builtins.match ".*TS_AUTHKEY.*" remoteYaml == null;
    assert builtins.match ".*pull_request:.*" remoteYaml == null;
    true;

  remoteDefaultsToFourWorkersAndTwoSlots =
    assert builtins.match ".*worker_count: 4.*" (flat (render { backend = "nix-remote"; })) != null;
    assert builtins.match ".*slots_per_worker: 2.*" (flat (render { backend = "nix-remote"; })) != null;
    true;

  remoteRuntimeSlotLimitMatchesGenerator =
    assert invalid (render (remote // { remote.slotsPerWorker = 0; }));
    assert invalid (render (remote // { remote.slotsPerWorker = 33; }));
    assert builtins.match ".*slots_per_worker: 32.*" (flat (render (remote // {
      remote.slotsPerWorker = 32;
    }))) != null;
    true;

  authkeyUsesOnlyTheKeyButPreservesCalledWorkflowPermissions =
    assert builtins.match ".*TS_AUTHKEY.*" authkeyYaml != null;
    assert builtins.match ".*TS_OAUTH_CLIENT_ID.*" authkeyYaml == null;
    assert builtins.match ".*id-token: write.*" authkeyYaml != null;
    true;

  remotePullRequestsRejected =
    assert invalid (mkWorkflow (base // {
      ci = remote;
      events.pullRequest = true;
    }));
    true;

  unsupportedBackendsAndCredentialsRejected =
    assert invalid (render { backend = "docker"; });
    assert invalid (render (remote // {
      remote.credentials = "token";
    }));
    assert invalid (render (remote // {
      remote.transport = "ssh";
    }));
    true;

  invalidWorkerRangesAndExtraneousOptionsRejected =
    assert invalid (render (remote // { remote.workers = 0; }));
    assert invalid (render (remote // { remote.workers = 17; }));
    assert invalid (render {
      backend = "matrix";
      remote.workers = 4;
    });
    true;

  invalidPinsPathsAndEventsRejected =
    assert invalid (mkWorkflow (base // {
      ci = remote;
      libraryRef = "main";
    }));
    assert invalid (mkWorkflow (base // {
      ci = remote;
      configPath = "../outside.nix";
    }));
    assert invalid (mkWorkflow (base // {
      ci = remote;
      events = { pullRequest = false; workflowDispatch = false; pushBranches = [ ]; };
    }));
    true;

  optionalCiLeavesExistingCheckPlanUnchanged =
    assert !(configDefault ? ci);
    assert configDefault.schemaVersion == 1;
    assert configRemote.schemaVersion == 1;
    assert configRemote.ci == remote;
    assert builtins.removeAttrs configRemote [ "ci" ] == configDefault;
    true;
}
