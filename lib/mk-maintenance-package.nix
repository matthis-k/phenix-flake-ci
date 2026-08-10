{
  pkgs,
  maintenance,
}:
let
  inherit (builtins)
    attrNames
    concatLists
    concatStringsSep
    isFunction
    isList
    map
    ;

  fail = message: throw "phenix-flake-ci: ${message}";

  collectRuntimeInputs =
    path: node:
    let
      raw = node.runtimeInputs or [ ];
      resolved = if isFunction raw then raw pkgs else raw;
      children = node.commands or { };
      nested = concatLists (
        map (name: collectRuntimeInputs (path ++ [ name ]) children.${name}) (attrNames children)
      );
    in
    if !isList resolved then
      fail "`${
        builtins.concatStringsSep " " ([ maintenance.name ] ++ path)
      }`: runtimeInputs must resolve to a list"
    else
      resolved ++ nested;

  runtimeInputs = concatLists (
    map (name: collectRuntimeInputs [ name ] maintenance.commands.${name}) (
      attrNames maintenance.commands
    )
  );

  basePackage = pkgs.writeShellApplication {
    inherit (maintenance) name;
    inherit runtimeInputs;
    text = maintenance.script;

    # Provider metadata may intentionally contain literal GitHub expressions such
    # as `${{ runner.temp }}`. They are emitted from single-quoted shell strings
    # specifically so the generated dispatcher does not expand them.
    excludeShellChecks = [ "SC2016" ];
  };

  package = basePackage.overrideAttrs (old: {
    passthru = (old.passthru or { }) // {
      phenixMaintenance = {
        schemaVersion = maintenance.ci.schemaVersion;
        commandName = maintenance.name;
        inherit (maintenance) ci gitHooks;
      };
    };
  });

  hookArgs = concatStringsSep " " maintenance.gitHooks.preCommit;

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

          if command -v ${maintenance.name} >/dev/null 2>&1; then
            ${maintenance.name} ${hookArgs}
          elif command -v nix >/dev/null 2>&1; then
            nix develop --command ${maintenance.name} ${hookArgs}
          else
            echo "nix is required to run the configured Phenix pre-commit maintenance" >&2
            exit 1
          fi

          # Re-stage only paths that were already part of this commit. Maintenance
          # may normalize other dirty files, but hooks must not capture them.
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
            if [[ -n "$current_hooks_path" && "$current_hooks_path" != "$phenix_hooks_dir" ]]; then
              git config --local phenix-flake-ci.previousHooksPath "$current_hooks_path"
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
