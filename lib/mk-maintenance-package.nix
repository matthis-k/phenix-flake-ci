{
  pkgs,
  maintenance,
  commandPath ? null,
}:
let
  inherit (builtins) concatStringsSep;

  graph = import ./maintenance-command-graph.nix {
    inherit maintenance pkgs;
  };

  runtimeInputs =
    if commandPath == null then graph.allRuntimeInputs else graph.runtimeInputsForPath commandPath;

  scriptFor = path:
    if path == null then
      maintenance.script
    else
      import ./render-scoped-maintenance.nix {
        inherit maintenance;
        paths = graph.pathsForPath path;
      };

  makePackage = path: inputs:
    pkgs.writeShellApplication {
      inherit (maintenance) name;
      runtimeInputs = inputs;
      text = scriptFor path;
      excludeShellChecks = [ "SC2016" ];
    };

  basePackage = makePackage commandPath runtimeInputs;
  package = basePackage.overrideAttrs (old: {
    passthru = (old.passthru or { }) // {
      phenixMaintenance = {
        schemaVersion = maintenance.ci.schemaVersion;
        commandName = maintenance.name;
        scopePath = commandPath;
        inherit (maintenance) ci gitHooks;
      };
    };
  });

  hookArgs = concatStringsSep " " maintenance.gitHooks.preCommit;
  hookCommandPackage =
    if maintenance.gitHooks.enabled then
      makePackage maintenance.gitHooks.preCommit (
        graph.runtimeInputsForPath maintenance.gitHooks.preCommit
      )
    else
      null;

  gitHooksPackage =
    if maintenance.gitHooks.enabled then
      pkgs.writeTextFile {
        name = "${maintenance.name}-git-hooks";
        destination = "/pre-commit";
        executable = true;
        text = ''
          #!/usr/bin/env bash
          set -euo pipefail

          repo_root="$(git rev-parse --show-toplevel)"
          cd "$repo_root"

          mapfile -d $'\0' staged_paths < <(git diff --cached --name-only --diff-filter=ACMR -z)

          if [[ -x ${hookCommandPackage}/bin/${maintenance.name} ]]; then
            ${hookCommandPackage}/bin/${maintenance.name} ${hookArgs}
          elif command -v nix >/dev/null 2>&1; then
            nix develop --command ${maintenance.name} ${hookArgs}
          else
            echo "nix is required to run the configured Phenix pre-commit maintenance" >&2
            exit 1
          fi

          for path in "''${staged_paths[@]}"; do
            if [[ -e "$path" || -L "$path" ]]; then
              git add -- "$path"
            fi
          done

          git diff --cached --check
        '';
      }
    else
      null;

  shellHook = ''
    if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      git_dir="$(git rev-parse --absolute-git-dir)"
      phenix_hooks_dir="$git_dir/phenix-flake-ci-hooks"
      current_hooks_path="$(git config --local --get core.hooksPath || true)"

      ${
        if maintenance.gitHooks.enabled then
          ''
            if [[ "$current_hooks_path" != "$phenix_hooks_dir" ]]; then
              if [[ -n "$current_hooks_path" ]]; then
                git config --local phenix-flake-ci.previousHooksPath "$current_hooks_path"
              else
                git config --local --unset-all phenix-flake-ci.previousHooksPath || true
              fi
            fi

            mkdir -p "$phenix_hooks_dir"
            cp ${gitHooksPackage}/pre-commit "$phenix_hooks_dir/pre-commit"
            chmod +x "$phenix_hooks_dir/pre-commit"
            git config --local core.hooksPath "$phenix_hooks_dir"
          ''
        else
          ''
            if [[ "$current_hooks_path" == "$phenix_hooks_dir" ]]; then
              previous_hooks_path="$(git config --local --get phenix-flake-ci.previousHooksPath || true)"
              if [[ -n "$previous_hooks_path" ]]; then
                git config --local core.hooksPath "$previous_hooks_path"
              else
                git config --local --unset-all core.hooksPath || true
              fi
            fi

            git config --local --unset-all phenix-flake-ci.previousHooksPath || true
            rm -rf "$phenix_hooks_dir"
          ''
      }
    fi
  '';
in
{
  inherit
    package
    runtimeInputs
    gitHooksPackage
    shellHook
    ;

  app = {
    type = "app";
    program = "${package}/bin/${maintenance.name}";
  };
}
