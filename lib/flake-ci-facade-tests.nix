# Pure Nix API contract: no network, no toolchain, no GitHub runner required.
let
  mk = import ./mk-flake-ci.nix;
  mkConfig = import ./mk-flake-check-config.nix;
  sha = "0123456789abcdef0123456789abcdef01234567";
  pkgs = {
    stdenv.hostPlatform.system = "x86_64-linux";
    python3 = "/nix/store/mock-python";
    nix = "/nix/store/mock-nix";
    git = "/nix/store/mock-git";
    coreutils = "/nix/store/mock-coreutils";
    diffutils = "/nix/store/mock-diffutils";
    lib.escapeShellArg = string: "'${string}'";
    writeShellApplication = attrs: attrs // { outPath = "/nix/store/mock-${attrs.name}"; };
    writeText = name: content: "/nix/store/mock-${name}";
    runCommand = name: attrs: script: {
      inherit name attrs script;
      outPath = "/nix/store/mock-${name}";
    };
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
  config = mkConfig { system = "x86_64-linux"; ci = policy; };
  make = args: mk ({ inherit pkgs config; libraryRef = sha; } // args);
  value = make { };
  flat = builtins.replaceStrings [ "\n" ] [ " " ];
  fail = args: !(builtins.tryEval (builtins.deepSeq (make args) true)).success;
in
{
  emitsOneLocalCommandAndPairedGithubWorkflows =
    assert builtins.attrNames value.apps == [ "ci" "ci-check-workflows" "ci-workflows" ];
    assert builtins.attrNames value.packages == [ "ci" "ci-workflows" ];
    assert value.apps.ci.program == "/nix/store/mock-flake-ci/bin/flake-ci";
    assert builtins.match ".*flake-ci-local.py.*--config 'ci/flake-check-config.nix'.*" (flat value.packages.ci.text) != null;
    assert builtins.match ".*flake-nix-builders.yml@${sha}.*" (flat value.workflows.trusted) != null;
    assert builtins.match ".*flake-dag-staged.yml@${sha}.*" (flat value.workflows.pullRequest) != null;
    assert builtins.match ".*flake-ci.yml.*flake-ci-pr.yml.*" (flat value.packages.ci-workflows.script) != null;
    true;

  localOnlyNeedsNoGithubPolicyOrLibraryPin =
    let
      local = mk { inherit pkgs; config = mkConfig { system = "x86_64-linux"; }; };
    in
    assert builtins.attrNames local.apps == [ "ci" ];
    assert builtins.attrNames local.packages == [ "ci" ];
    assert local.workflows == { };
    true;

  configurableFilenamesAreUsedForAllGeneratedOutputs =
    let
      custom = make {
        workflowNames = { trusted = "trusted.yaml"; pullRequest = "pr.yaml"; };
      };
    in
    assert builtins.match ".*trusted.yaml.*pr.yaml.*" (flat custom.packages.ci-workflows.script) != null;
    true;

  apiRejectsUnsafeAndMismatchedInputs =
    assert fail { configPath = "../outside.nix"; };
    assert fail { configPath = "/etc/passwd"; };
    assert fail { workflowNames = { trusted = "../../bad.yml"; pullRequest = "pr.yml"; }; };
    assert fail { workflowNames = { trusted = "same.yml"; pullRequest = "same.yml"; }; };
    assert fail { libraryRef = "main"; };
    assert fail { config = config // { system = "aarch64-linux"; }; };
    assert fail { config = config // { schemaVersion = 2; }; };
    assert fail { config = (mkConfig { system = "x86_64-linux"; }); github = true; };
    true;
}
