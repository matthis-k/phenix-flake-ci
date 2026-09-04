{
  pkgs,
  maintenance,
  commandPath ? null,
}:
let
  inherit (builtins)
    concatLists
    concatStringsSep
    elem
    filter
    foldl'
    head
    listToAttrs
    map
    replaceStrings
    tail
    toJSON
    ;

  graph = import ./maintenance-command-graph.nix {
    inherit maintenance pkgs;
  };

  pathId = path: concatStringsSep "/" path;
  shellQuote = value: "'${replaceStrings [ "'" ] [ "'\"'\"'" ] value}'";
  unique = foldl' (items: item: if elem item items then items else items ++ [ item ]) [ ];

  prefixes =
    path:
    let
      go =
        prefix: remaining:
        if remaining == [ ] then
          [ ]
        else
          let
            next = prefix ++ [ (head remaining) ];
          in
          [ next ] ++ go next (tail remaining);
    in
    go [ ] path;

  availablePathsFor =
    path:
    if path == null then
      graph.commandPaths
    else
      unique (concatLists (map prefixes (graph.pathsForPath path)));

  logicalCommandsFor =
    path:
    let
      availableIds = map pathId (availablePathsFor path);
      commandIds = filter (id: elem id availableIds) maintenance.index.commandOrder;
    in
    {
      inherit commandIds;
      commands = listToAttrs (
        map (
          id:
          let
            command = maintenance.index.commands.${id};
          in
          {
            name = id;
            value = command // {
              children = filter (child: elem child availableIds) command.children;
              dependencies = filter (dependency: elem dependency availableIds) command.dependencies;
            };
          }
        ) commandIds
      );
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

  implementationName = "${maintenance.name}-impl";

  materialize =
    path: inputs:
    let
      logical = logicalCommandsFor path;
      implementation = pkgs.writeShellApplication {
        name = implementationName;
        runtimeInputs = inputs;
        text = scriptFor path;
        excludeShellChecks = [ "SC2016" ];
      };
      implementationProgram = "${implementation}/bin/${implementationName}";
      indexedCommands = listToAttrs (
        map (
          id:
          let
            command = logical.commands.${id};
          in
          {
            name = id;
            value = command // {
              execution = {
                program = implementationProgram;
                args = command.path;
              };
            };
          }
        ) logical.commandIds
      );
      packageIndex = maintenance.index // {
        scope = if path == null then null else { command = pathId path; };
        commandOrder = logical.commandIds;
        commands = indexedCommands;
        ci =
          if path == null then
            maintenance.index.ci
          else
            {
              schemaVersion = maintenance.index.ci.schemaVersion;
              jobs = [ ];
              matrix = { include = [ ]; };
            };
        hooks = if path == null then maintenance.index.hooks else { };
      };
      indexFile = pkgs.writeText "${maintenance.name}-execution-index.json" (toJSON packageIndex);
      wrapped = pkgs.writeShellApplication {
        inherit (maintenance) name;
        runtimeInputs = [
          pkgs.coreutils
          pkgs.jq
          implementation
        ];
        excludeShellChecks = [ "SC2016" ];
        text = ''
          index_file=${indexFile}
          implementation_program=${implementationProgram}

          invocation_error() {
            jq -cn \
              --arg kind "$1" \
              --arg message "$2" \
              '{type:"error",kind:$kind,message:$message}' >&2
            exit 2
          }

          run_indexed_command() {
            local command_id="$1"
            shift

            local program
            if ! program="$(
              jq -er --arg id "$command_id" '.commands[$id].execution.program // empty' "$index_file"
            )"; then
              invocation_error "unknown_command" "Command is not present in this executable index: $command_id"
            fi

            local -a declared_args
            mapfile -d $'\0' -t declared_args < <(
              jq -j --arg id "$command_id" '.commands[$id].execution.args[] | ., "\u0000"' "$index_file"
            )

            exec "$program" "''${declared_args[@]}" "$@"
          }

          resolve_cli_command() {
            local resolved
            if ! resolved="$(
              jq -ner \
                --slurpfile index "$index_file" \
                --args '
                  $index[0] as $index
                  | $ARGS.positional as $argv
                  | [
                      range(1; ($argv | length) + 1) as $consumed
                      | ($argv[0:$consumed] | join("/")) as $id
                      | select($index.commands[$id] != null)
                      | { id: $id, consumed: $consumed }
                    ]
                  | last
                  | "\(.id)\t\(.consumed)"
                ' \
                -- "$@"
            )"; then
              return 1
            fi

            IFS=$'\t' read -r cli_command_id cli_consumed <<< "$resolved"
          }

          case "''${1:-}" in
            index)
              if (( $# != 1 )); then
                invocation_error "unexpected_arguments" "Usage: ${maintenance.name} index"
              fi
              cat "$index_file"
              ;;
            invoke)
              if (( $# != 1 )); then
                invocation_error "unexpected_arguments" "Usage: ${maintenance.name} invoke < invocation.json"
              fi

              invocation="$(cat)"
              if ! command_id="$(
                printf '%s' "$invocation" |
                  jq -er '
                    if type == "object"
                      and (.command | type == "string")
                      and (.command | length > 0)
                    then .command
                    else error("command must be a non-empty string")
                    end
                  '
              )"; then
                invocation_error "invalid_invocation" "Invocation must contain a non-empty string command"
              fi

              if ! printf '%s' "$invocation" | jq -e '
                (.args // []) as $args |
                ($args | type == "array") and all($args[]; type == "string")
              ' >/dev/null; then
                invocation_error "invalid_invocation" "Invocation args must be an array of strings"
              fi

              mapfile -d $'\0' -t invocation_args < <(
                printf '%s' "$invocation" | jq -j '(.args // [])[] | ., "\u0000"'
              )

              run_indexed_command "$command_id" "''${invocation_args[@]}"
              ;;
            *)
              if resolve_cli_command "$@"; then
                shift "$cli_consumed"
                run_indexed_command "$cli_command_id" "$@"
              fi
              exec "$implementation_program" "$@"
              ;;
          esac
        '';
      };
      package = wrapped.overrideAttrs (old: {
        passthru = (old.passthru or { }) // {
          phenixMaintenance = {
            schemaVersion = maintenance.ci.schemaVersion;
            commandName = maintenance.name;
            scopePath = path;
            index = packageIndex;
            inherit indexFile;
            inherit (maintenance) ci gitHooks;
          };
        };
      });
    in
    {
      inherit
        package
        packageIndex
        indexFile
        implementation
        ;
    };

  materialized = materialize commandPath runtimeInputs;
  package = materialized.package;

  hookCommandId = pathId maintenance.gitHooks.preCommit;
  hookInvocation = toJSON {
    command = hookCommandId;
    source = {
      type = "git-hook";
      hook = "pre-commit";
    };
  };
  hookMaterialized =
    if maintenance.gitHooks.enabled then
      materialize maintenance.gitHooks.preCommit (
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

          if [[ -x ${hookMaterialized.package}/bin/${maintenance.name} ]]; then
            printf '%s\n' ${shellQuote hookInvocation} |
              ${hookMaterialized.package}/bin/${maintenance.name} invoke
          elif command -v nix >/dev/null 2>&1; then
            printf '%s\n' ${shellQuote hookInvocation} |
              nix develop --command ${maintenance.name} invoke
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
