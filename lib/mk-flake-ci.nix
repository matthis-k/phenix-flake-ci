# Nix-first facade over the existing exact-head flake CI machinery.
# No new inventory, scheduler, cache, test runner, or GitHub workflow engine.
{
  pkgs,
  config,
  configPath ? "ci/flake-check-config.nix",
  libraryRef ? null,
  github ? (config ? ci),
  workflowNames ? {
    trusted = "flake-ci.yml";
    pullRequest = "flake-ci-pr.yml";
  },
  name ? "Flake CI",
}:
let
  fail = message: throw "phenix-flake-ci.mkFlakeCI: ${message}";
  lib = pkgs.lib;
  validPath = path:
    builtins.isString path
    && builtins.match "[A-Za-z0-9_./-]+" path != null
    && builtins.substring 0 1 path != "/"
    && !(builtins.elem ".." (builtins.split "/" path));
  validWorkflowName = value:
    builtins.isString value
    && builtins.match "[A-Za-z0-9][A-Za-z0-9_.-]*[.](yml|yaml)" value != null;
  validNames =
    builtins.isAttrs workflowNames
    && builtins.attrNames workflowNames == [ "pullRequest" "trusted" ]
    && builtins.all validWorkflowName (builtins.attrValues workflowNames)
    && workflowNames.trusted != workflowNames.pullRequest;
  valid =
    if !builtins.isAttrs config || (config.schemaVersion or null) != 1
      || !builtins.isString (config.system or null) then
      fail "config must be a version-1 mkFlakeCheckConfig result"
    else if config.system != pkgs.stdenv.hostPlatform.system then
      fail "config.system must match pkgs.stdenv.hostPlatform.system"
    else if !validPath configPath then
      fail "configPath must be a repository-relative Nix file path"
    else if !builtins.isBool github then
      fail "github must be a boolean"
    else if !builtins.isString name || name == "" then
      fail "name must be nonempty"
    else if github && !(config ? ci) then
      fail "GitHub workflows require config.ci"
    else if github && (!validNames || libraryRef == null) then
      fail "GitHub workflows require distinct safe filenames and a pinned libraryRef"
    else
      true;

  # Existing Nix workflow generator owns trust rules, Tailscale admission,
  # reusable workflow pins and the staged vs native Nix backend choice.
  workflows =
    if github then
      import ./mk-flake-ci-workflows.nix {
        ci = config.ci;
        inherit configPath libraryRef name;
      }
    else
      { };

  # Keep the already-tested Python helpers together: flake-ci-local.py imports
  # its siblings relative to __file__. No new orchestrator is introduced.
  localCommand = pkgs.writeShellApplication {
    name = "flake-ci";
    runtimeInputs = [ pkgs.python3 pkgs.nix pkgs.git ];
    text = ''
      exec ${pkgs.python3}/bin/python3 ${./.}/flake-ci-local.py \
        --config ${lib.escapeShellArg configPath} "$@"
    '';
  };

  app = command: {
    type = "app";
    program = "${command}/bin/${command.name}";
  };

  trustedFile = pkgs.writeText workflowNames.trusted workflows.trusted;
  prFile = pkgs.writeText workflowNames.pullRequest workflows.pullRequest;
  workflowDirectory = ".github/workflows";
  workflowPackage = pkgs.runCommand "flake-ci-workflows" { } ''
    mkdir -p "$out/${workflowDirectory}"
    install -m 0644 ${trustedFile} "$out/${workflowDirectory}/${workflowNames.trusted}"
    install -m 0644 ${prFile} "$out/${workflowDirectory}/${workflowNames.pullRequest}"
  '';
  workflowSync = pkgs.writeShellApplication {
    name = "flake-ci-sync-workflows";
    runtimeInputs = [ pkgs.coreutils pkgs.git ];
    text = ''
      if [[ "$PWD" != "$(git rev-parse --show-toplevel)" ]]; then
        echo "run workflow synchronization from the repository root" >&2
        exit 2
      fi
      mkdir -p ${workflowDirectory}
      install -m 0644 ${trustedFile} ${workflowDirectory}/${workflowNames.trusted}
      install -m 0644 ${prFile} ${workflowDirectory}/${workflowNames.pullRequest}
    '';
  };
  workflowCheck = pkgs.writeShellApplication {
    name = "flake-ci-check-workflows";
    runtimeInputs = [ pkgs.diffutils pkgs.git ];
    text = ''
      if [[ "$PWD" != "$(git rev-parse --show-toplevel)" ]]; then
        echo "run workflow verification from the repository root" >&2
        exit 2
      fi
      diff -u ${trustedFile} ${workflowDirectory}/${workflowNames.trusted}
      diff -u ${prFile} ${workflowDirectory}/${workflowNames.pullRequest}
    '';
  };
in
assert valid;
{
  inherit config workflows;
  packages = {
    ci = localCommand;
  } // (if github then { ci-workflows = workflowPackage; } else { });
  apps = {
    ci = app localCommand;
  } // (if github then {
    ci-workflows = app workflowSync;
    ci-check-workflows = app workflowCheck;
  } else { });
}
