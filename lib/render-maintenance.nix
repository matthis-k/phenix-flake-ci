{
  name ? "maintenance",
  description ? "Repository maintenance commands",
  commands,
}:
let
  inherit (builtins)
    attrNames
    concatLists
    concatStringsSep
    elem
    filter
    foldl'
    hasAttr
    isAttrs
    isBool
    isFunction
    isInt
    isList
    isString
    length
    map
    match
    replaceStrings
    sort
    toJSON
    ;

  fail = message: throw "phenix-flake-ci: ${message}";

  validCommandName =
    commandName: isString commandName && match "^[A-Za-z0-9][A-Za-z0-9_-]*$" commandName != null;

  validStageName =
    stageName: isString stageName && match "^[A-Za-z0-9][A-Za-z0-9_-]*$" stageName != null;

  shellQuote = value: "'${replaceStrings [ "'" ] [ "'\"'\"'" ] value}'";

  pathId = path: concatStringsSep "/" path;

  functionId = path: "n_${builtins.hashString "sha256" (pathId path)}";
  displayPath = path: concatStringsSep " " ([ name ] ++ path);

  childNames =
    path: node:
    let
      children = node.commands or { };
      names = attrNames children;
      order = node.order or names;
      sortedOrder = sort builtins.lessThan order;
    in
    if !isAttrs children then
      fail "`${displayPath path}`: commands must be an attribute set"
    else if !isList order || !(builtins.all isString order) then
      fail "`${displayPath path}`: order must be a list of command names"
    else if length sortedOrder != length names || sortedOrder != names then
      fail "`${displayPath path}`: order must contain every child command exactly once"
    else
      order;

  normalizeCi =
    node:
    let
      raw = node.ci or { };
    in
    if isBool raw then { enable = raw; } else raw;

  validateCi =
    path: node:
    let
      ci = normalizeCi node;
    in
    if !isAttrs ci then
      fail "`${displayPath path}`: ci must be a boolean or attribute set"
    else if hasAttr "enable" ci && !isBool ci.enable then
      fail "`${displayPath path}`: ci.enable must be a boolean"
    else if hasAttr "stage" ci && !validStageName ci.stage then
      fail "`${displayPath path}`: ci.stage must be a simple stage identifier"
    else if hasAttr "name" ci && !isString ci.name then
      fail "`${displayPath path}`: ci.name must be a string"
    else if hasAttr "stepName" ci && !isString ci.stepName then
      fail "`${displayPath path}`: ci.stepName must be a string"
    else if hasAttr "runner" ci && !isString ci.runner then
      fail "`${displayPath path}`: ci.runner must be a string"
    else if hasAttr "timeoutMinutes" ci && (!isInt ci.timeoutMinutes || ci.timeoutMinutes <= 0) then
      fail "`${displayPath path}`: ci.timeoutMinutes must be a positive integer"
    else if hasAttr "needs" ci && (!isList ci.needs || !(builtins.all validStageName ci.needs)) then
      fail "`${displayPath path}`: ci.needs must be a list of simple stage identifiers"
    else if hasAttr "env" ci && !isAttrs ci.env then
      fail "`${displayPath path}`: ci.env must be an attribute set"
    else if
      hasAttr "env" ci
      && !(builtins.all (name: match "^[A-Za-z_][A-Za-z0-9_]*$" name != null && isString ci.env.${name}) (
        attrNames ci.env
      ))
    then
      fail "`${displayPath path}`: ci.env keys must be environment variable names and values must be strings"
    else
      true;

  validateNode =
    path: node:
    let
      hasExec = hasAttr "exec" node;
      children = node.commands or { };
      names = if isAttrs children then childNames path node else [ ];
      hasChildren = names != [ ];
      descriptionValue = node.description or "";
      runtimeInputsValue = node.runtimeInputs or [ ];
      ciValid = validateCi path node;
    in
    if !isAttrs node then
      fail "`${displayPath path}`: command definition must be an attribute set"
    else if !isString descriptionValue then
      fail "`${displayPath path}`: description must be a string"
    else if !(isList runtimeInputsValue || isFunction runtimeInputsValue) then
      fail "`${displayPath path}`: runtimeInputs must be a list or a pkgs function"
    else if hasExec && !isString node.exec then
      fail "`${displayPath path}`: exec must be a shell script string"
    else if hasExec == hasChildren then
      fail "`${displayPath path}` must define exactly one of exec or non-empty commands"
    else if hasAttr "order" node && !hasChildren then
      fail "`${displayPath path}`: order is only valid for commands with children"
    else if !ciValid then
      false
    else
      builtins.all (
        childName:
        if !validCommandName childName then
          fail "`${displayPath path}`: invalid command name `${childName}`"
        else
          validateNode (path ++ [ childName ]) children.${childName}
      ) names;

  rootNames = attrNames commands;

  validated =
    if !isString name || match "^[A-Za-z0-9][A-Za-z0-9_-]*$" name == null then
      fail "name must be a shell-safe command name"
    else if !isString description then
      fail "description must be a string"
    else if !isAttrs commands || rootNames == [ ] then
      fail "commands must be a non-empty attribute set"
    else
      builtins.all (
        commandName:
        if !validCommandName commandName || commandName == "ci" then
          fail "invalid or reserved top-level command name `${commandName}`"
        else
          validateNode [ commandName ] commands.${commandName}
      ) rootNames;

  flattenNode =
    path: node:
    let
      names = if hasAttr "commands" node then childNames path node else [ ];
    in
    [ { inherit path node; } ]
    ++ concatLists (
      map (childName: flattenNode (path ++ [ childName ]) node.commands.${childName}) names
    );

  flattened = concatLists (
    map (commandName: flattenNode [ commandName ] commands.${commandName}) rootNames
  );
  ciEntries = filter (entry: ((normalizeCi entry.node).enable or false)) flattened;

  entryStage =
    entry:
    let
      ci = normalizeCi entry.node;
      commandId = pathId entry.path;
      descriptionValue = entry.node.description or "";
      stageId = ci.stage or commandId;
      fallbackName = if descriptionValue == "" then commandId else descriptionValue;
      jobName = ci.name or (if hasAttr "stage" ci then stageId else fallbackName);
      stepName = ci.stepName or fallbackName;
    in
    {
      id = stageId;
      meta = {
        name = jobName;
        runner = ci.runner or "ubuntu-latest";
        timeout = ci.timeoutMinutes or 30;
        needs = ci.needs or [ ];
        env = ci.env or { };
      };
      command = {
        id = commandId;
        inherit (entry) path;
        name = stepName;
      };
      inherit entry;
    };

  stagedEntries = map entryStage ciEntries;

  stageOrder = foldl' (
    order: staged: if elem staged.id order then order else order ++ [ staged.id ]
  ) [ ] stagedEntries;

  stageMap = foldl' (
    acc: staged:
    let
      existing = acc.${staged.id} or null;
    in
    if existing == null then
      acc
      // {
        ${staged.id} = {
          inherit (staged) meta;
          entries = [ staged.entry ];
          commands = [ staged.command ];
        };
      }
    else if existing.meta != staged.meta then
      fail "CI stage `${staged.id}` has conflicting name/runner/timeout/needs/env metadata"
    else
      acc
      // {
        ${staged.id} = existing // {
          entries = existing.entries ++ [ staged.entry ];
          commands = existing.commands ++ [ staged.command ];
        };
      }
  ) { } stagedEntries;

  jobs = map (
    stageId:
    let
      stage = stageMap.${stageId};
    in
    {
      id = stageId;
      inherit (stage.meta)
        name
        runner
        timeout
        needs
        env
        ;
      inherit (stage) commands entries;
    }
  ) stageOrder;

  jobsValid = builtins.all (
    job:
    builtins.all (
      need:
      if need == job.id then
        fail "CI stage `${job.id}` cannot depend on itself"
      else if !(elem need stageOrder) then
        fail "CI stage `${job.id}` depends on unknown stage `${need}`"
      else
        true
    ) job.needs
  ) jobs;

  matrixJobs = map (job: {
    inherit (job)
      id
      name
      runner
      timeout
      needs
      env
      commands
      ;
  }) jobs;

  publicJobs = matrixJobs;
  matrix = {
    include = publicJobs;
  };
  matrixJson = toJSON matrix;

  renderHelp =
    path: node:
    let
      id = functionId path;
      names = if hasAttr "commands" node then childNames path node else [ ];
      usage = if names == [ ] then "${displayPath path} [args...]" else "${displayPath path} <command>";
      commandRows = concatStringsSep "\n" (
        map (
          childName:
          let
            child = node.commands.${childName};
          in
          "  printf '  %-20s %s\\n' ${shellQuote childName} ${shellQuote (child.description or "")}"
        ) names
      );
    in
    ''
      help_${id}() {
        printf '%s\n' ${shellQuote (node.description or "")}
        printf '\nUsage: %s\n' ${shellQuote usage}
        ${
          if names == [ ] then
            ""
          else
            ''
              printf '\nCommands:\n'
              ${commandRows}
            ''
        }
      }
    '';

  renderRun =
    path: node:
    let
      id = functionId path;
      names = if hasAttr "commands" node then childNames path node else [ ];
    in
    if hasAttr "exec" node then
      ''
        run_${id}() {
          ${node.exec}
        }
      ''
    else
      ''
        run_${id}() {
          ${concatStringsSep "\n" (map (childName: "run_${functionId (path ++ [ childName ])}") names)}
        }
      '';

  renderDispatch =
    path: node:
    let
      id = functionId path;
      names = if hasAttr "commands" node then childNames path node else [ ];
      cases = concatStringsSep "\n" (
        map (childName: ''
          ${shellQuote childName})
            shift
            dispatch_${functionId (path ++ [ childName ])} "$@"
            ;;
        '') names
      );
    in
    if names == [ ] then
      ''
        dispatch_${id}() {
          case "''${1:-}" in
            -h|--help)
              help_${id}
              ;;
            *)
              run_${id} "$@"
              ;;
          esac
        }
      ''
    else
      ''
        dispatch_${id}() {
          case "''${1:-}" in
            "")
              run_${id}
              ;;
            -h|--help)
              help_${id}
              ;;
            ${cases}
            *)
              printf 'Unknown command for %s: %s\n\n' ${shellQuote (displayPath path)} "$1" >&2
              help_${id} >&2
              return 2
              ;;
          esac
        }
      '';

  renderNode =
    entry:
    let
      inherit (entry) path;
      inherit (entry) node;
    in
    renderHelp path node + renderRun path node + renderDispatch path node;

  rootHelpRows = concatStringsSep "\n" (
    map (
      commandName:
      "  printf '  %-20s %s\\n' ${shellQuote commandName} ${
          shellQuote (commands.${commandName}.description or "")
        }"
    ) rootNames
  );

  rootCases = concatStringsSep "\n" (
    map (commandName: ''
      ${shellQuote commandName})
        shift
        dispatch_${functionId [ commandName ]} "$@"
        ;;
    '') rootNames
  );

  ciRunCases = concatStringsSep "\n" (
    map (
      stage:
      let
        runs = concatStringsSep "\n" (
          map (
            entry:
            let
              args = concatStringsSep " " (map shellQuote entry.path);
            in
            "printf '==> %s\\n' ${shellQuote (displayPath entry.path)} >&2\ndispatch_root ${args}"
          ) stage.entries
        );
      in
      ''
        ${shellQuote stage.id})
          ${runs}
          ;;
      ''
    ) jobs
  );

  ciListRows = concatStringsSep "\n" (
    map (stage: "  printf '%-24s %s\\n' ${shellQuote stage.id} ${shellQuote stage.name}") jobs
  );

  script = ''
    help_root() {
      printf '%s\n' ${shellQuote description}
      printf '\nUsage: %s <command>\n' ${shellQuote name}
      printf '\nCommands:\n'
      ${rootHelpRows}
      printf '  %-20s %s\n' 'ci' 'Inspect or run CI-exposed stages'
    }

    ${concatStringsSep "\n" (map renderNode flattened)}

    ci_help() {
      printf '%s\n' 'CI discovery for flake-maintenance providers'
      printf '\nUsage: %s ci <command>\n' ${shellQuote name}
      printf '\nCommands:\n'
      printf '  %-20s %s\n' 'matrix' 'Emit the GitHub Actions matrix as JSON'
      printf '  %-20s %s\n' 'list' 'List CI-exposed stages'
      printf '  %-20s %s\n' 'run <id>' 'Run one CI stage by stable stage ID'
    }

    ci_dispatch() {
      case "''${1:-}" in
        matrix)
          printf '%s\n' ${shellQuote matrixJson}
          ;;
        list)
          ${if jobs == [ ] then "printf '%s\\n' 'No CI stages are enabled.'" else ciListRows}
          ;;
        run)
          if [[ $# -ne 2 ]]; then
            printf '%s\n' 'Usage: ${name} ci run <id>' >&2
            return 2
          fi
          case "$2" in
            ${ciRunCases}
            *)
              printf 'Unknown CI stage: %s\n' "$2" >&2
              return 2
              ;;
          esac
          ;;
        ""|-h|--help)
          ci_help
          ;;
        *)
          printf 'Unknown CI command: %s\n\n' "$1" >&2
          ci_help >&2
          return 2
          ;;
      esac
    }

    dispatch_root() {
      case "''${1:-}" in
        ""|-h|--help)
          help_root
          ;;
        ci)
          shift
          ci_dispatch "$@"
          ;;
        ${rootCases}
        *)
          printf 'Unknown command: %s\n\n' "$1" >&2
          help_root >&2
          return 2
          ;;
      esac
    }

    dispatch_root "$@"
  '';
in
assert validated;
assert jobsValid;
{
  inherit
    script
    matrix
    jobs
    publicJobs
    ;
}
