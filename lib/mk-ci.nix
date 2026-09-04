{
  build ? { },
  test ? { },
  runtime ? { },
  integration ? { },
  product ? { },
  ci ? { },
}:
let
  inherit (builtins)
    attrNames
    concatLists
    concatStringsSep
    elem
    elemAt
    filter
    foldl'
    isAttrs
    isBool
    isFunction
    isList
    isString
    length
    listToAttrs
    map
    match
    replaceStrings
    ;

  fail = message: throw "phenix-flake-ci: ${message}";
  shellQuote = value: "'${replaceStrings [ "'" ] [ "'\"'\"'" ] value}'";

  phases = [
    {
      id = "build";
      name = "Build";
      description = "Build deployable artifacts";
      suites = build;
    }
    {
      id = "test";
      name = "Test";
      description = "Run code-level tests";
      suites = test;
    }
    {
      id = "runtime";
      name = "Runtime";
      description = "Exercise built artifacts as running programs";
      suites = runtime;
    }
    {
      id = "integration";
      name = "Integration";
      description = "Exercise interactions between built artifacts";
      suites = integration;
    }
    {
      id = "product";
      name = "Product";
      description = "Exercise supported user journeys";
      suites = product;
    }
  ];

  cache = ci.cache or null;

  ciValid =
    if !isAttrs ci then
      fail "mkCi ci must be an attribute set"
    else if builtins.hasAttr "stepName" ci then
      fail "mkCi ci.stepName is suite-owned"
    else if builtins.hasAttr "enable" ci then
      fail "mkCi ci.enable is always true"
    else if builtins.hasAttr "stage" ci then
      fail "mkCi ci.stage is suite-owned"
    else if cache != null && !isAttrs cache then
      fail "mkCi ci.cache must be an attribute set"
    else if cache != null && (!builtins.hasAttr "paths" cache || !isList cache.paths || cache.paths == [ ] || !(builtins.all isString cache.paths)) then
      fail "mkCi ci.cache.paths must be a non-empty list of strings"
    else if cache != null && (!builtins.hasAttr "key" cache || !isString cache.key) then
      fail "mkCi ci.cache.key must be a string"
    else if cache != null && builtins.hasAttr "restoreKeys" cache && (!isList cache.restoreKeys || !(builtins.all isString cache.restoreKeys)) then
      fail "mkCi ci.cache.restoreKeys must be a list of strings"
    else
      true;

  normalizeSuite =
    phase: suiteName: suite:
    let
      description = suite.description or suiteName;
      label = suite.name or description;
      quiet = suite.quiet or true;
      runtimeInputs = suite.runtimeInputs or [ ];
      exec = suite.exec or null;
      needs = suite.needs or null;
      useCache = suite.cache or true;
    in
    if !isAttrs suite then
      fail "mkCi ${phase.id}.${suiteName} must be an attribute set"
    else if match "^[A-Za-z0-9][A-Za-z0-9_-]*$" suiteName == null then
      fail "mkCi ${phase.id} has invalid suite name `${suiteName}`"
    else if !isString description then
      fail "mkCi ${phase.id}.${suiteName}.description must be a string"
    else if !isString label then
      fail "mkCi ${phase.id}.${suiteName}.name must be a string"
    else if !isBool quiet then
      fail "mkCi ${phase.id}.${suiteName}.quiet must be a boolean"
    else if !(isList runtimeInputs || isFunction runtimeInputs) then
      fail "mkCi ${phase.id}.${suiteName}.runtimeInputs must be a list or a pkgs function"
    else if !isString exec then
      fail "mkCi ${phase.id}.${suiteName}.exec must be a shell script string"
    else if needs != null && (!isList needs || !(builtins.all isString needs)) then
      fail "mkCi ${phase.id}.${suiteName}.needs must be a list of `phase.suite` strings"
    else if !isBool useCache then
      fail "mkCi ${phase.id}.${suiteName}.cache must be a boolean"
    else
      {
        inherit
          suiteName
          description
          label
          quiet
          runtimeInputs
          exec
          needs
          useCache
          ;
        phaseId = phase.id;
        phaseName = phase.name;
        taskId = "${phase.id}-${suiteName}";
      };

  normalizeSuites =
    phase:
    if !isAttrs phase.suites then
      fail "mkCi ${phase.id} must be an attribute set of suites"
    else
      map (suiteName: normalizeSuite phase suiteName phase.suites.${suiteName}) (attrNames phase.suites);

  normalizedPhases = foldl'
    (
      acc: phase:
      let
        suites = normalizeSuites phase;
      in
      if suites == [ ] then
        acc
      else
        acc ++ [ (phase // { inherit suites; }) ]
    )
    [ ]
    phases;

  allSuites = concatLists (map (phase: phase.suites) normalizedPhases);
  buildRefs = map (suite: "${suite.phaseId}.${suite.suiteName}") (filter (suite: suite.phaseId == "build") allSuites);

  parseNeed =
    raw:
    let
      parts = match "^([A-Za-z0-9][A-Za-z0-9_-]*)\\.([A-Za-z0-9][A-Za-z0-9_-]*)$" raw;
    in
    if parts == null then
      fail "mkCi dependency `${raw}` must use `phase.suite`"
    else
      {
        phaseId = elemAt parts 0;
        suiteName = elemAt parts 1;
        taskId = "${elemAt parts 0}-${elemAt parts 1}";
        ref = raw;
      };

  suitesWithNeeds = map (
    suite:
    let
      rawNeeds =
        if suite.needs != null then
          suite.needs
        else if suite.phaseId == "build" then
          [ ]
        else
          buildRefs;
    in
    suite // { needs = map parseNeed rawNeeds; }
  ) allSuites;

  taskIds = map (suite: suite.taskId) suitesWithNeeds;
  taskMap = listToAttrs (map (suite: {
    name = suite.taskId;
    value = suite;
  }) suitesWithNeeds);

  needsValid = builtins.all (
    suite:
    builtins.all (
      need:
      if !elem need.taskId taskIds then
        fail "mkCi ${suite.phaseId}.${suite.suiteName} depends on unknown suite `${need.ref}`"
      else if need.taskId == suite.taskId then
        fail "mkCi ${suite.phaseId}.${suite.suiteName} cannot depend on itself"
      else
        true
    ) suite.needs
  ) suitesWithNeeds;

  reaches =
    start: current: seen:
    if elem current seen then
      false
    else
      builtins.any (
        need:
        need.taskId == start || reaches start need.taskId (seen ++ [ current ])
      ) taskMap.${current}.needs;

  acyclic = builtins.all (
    suite:
    if reaches suite.taskId suite.taskId [ ] then
      fail "mkCi dependency cycle reaches `${suite.phaseId}.${suite.suiteName}`"
    else
      true
  ) suitesWithNeeds;

  renderSuccess =
    suite:
    let
      phase = shellQuote suite.phaseId;
      suiteName = shellQuote suite.suiteName;
      label = shellQuote suite.label;
      withOutput = ''
        jq -cn \
          --arg phase ${phase} \
          --arg suite ${suiteName} \
          --arg name ${label} \
          --rawfile output "$phenix_ci_log" \
          '{type:"suite",phase:$phase,suite:$suite,name:$name,status:"pass",output:$output}'
      '';
      quietOutput = ''
        if [[ "''${PHENIX_CI_VERBOSE:-0}" == "1" ]]; then
          ${withOutput}
        else
          jq -cn \
            --arg phase ${phase} \
            --arg suite ${suiteName} \
            --arg name ${label} \
            '{type:"suite",phase:$phase,suite:$suite,name:$name,status:"pass"}'
        fi
      '';
    in
    if suite.quiet then quietOutput else withOutput;

  renderSuiteExec =
    suite:
    let
      phase = shellQuote suite.phaseId;
      suiteName = shellQuote suite.suiteName;
      label = shellQuote suite.label;
      success = renderSuccess suite;
    in
    ''
      if [[ "''${1:-}" == "--verbose" ]]; then
        export PHENIX_CI_VERBOSE=1
        shift
      fi

      if (( $# > 0 )); then
        jq -cn \
          --arg phase ${phase} \
          --arg suite ${suiteName} \
          '{type:"error",kind:"unexpected_arguments",phase:$phase,suite:$suite}'
        return 2
      fi

      phenix_ci_log="$(mktemp)"
      if (
        ${suite.exec}
      ) >"$phenix_ci_log" 2>&1; then
        ${success}
        rm -f "$phenix_ci_log"
        return 0
      else
        phenix_ci_status=$?
        jq -cn \
          --arg phase ${phase} \
          --arg suite ${suiteName} \
          --arg name ${label} \
          --argjson exit_code "$phenix_ci_status" \
          --rawfile output "$phenix_ci_log" \
          '{type:"suite",phase:$phase,suite:$suite,name:$name,status:"fail",exit_code:$exit_code,output:$output}'
        rm -f "$phenix_ci_log"
        return "$phenix_ci_status"
      fi
    '';

  suiteRuntimeInputs =
    suite: pkgs:
    [
      pkgs.coreutils
      pkgs.jq
    ]
    ++ (if isFunction suite.runtimeInputs then suite.runtimeInputs pkgs else suite.runtimeInputs);

  globalNeeds = ci.needs or [ ];
  globalEnv = ci.env or { };
  globalRunner = ci.runner or "ubuntu-latest";
  globalTimeout = ci.timeoutMinutes or 30;
  jobPrefix = ci.name or null;

  suiteJobName =
    suite:
    let
      semanticName = "${suite.phaseName} / ${suite.label}";
    in
    if jobPrefix == null then semanticName else "${jobPrefix} / ${semanticName}";

  suiteCommands = listToAttrs (map (
    suite:
    {
      name = suite.taskId;
      value = {
        description = suite.label;
        runtimeInputs = suiteRuntimeInputs suite;
        exec = renderSuiteExec suite;
        ci = {
          enable = true;
          stage = suite.taskId;
          name = suiteJobName suite;
          stepName = suite.label;
          runner = globalRunner;
          timeoutMinutes = globalTimeout;
          needs = globalNeeds ++ map (need: need.taskId) suite.needs;
          env = globalEnv;
          cache = if suite.useCache then cache else null;
        };
      };
    }
  ) suitesWithNeeds);

  suitesForPhase =
    phaseId: filter (suite: suite.phaseId == phaseId) suitesWithNeeds;

  renderListRow =
    suite:
    ''
      jq -cn \
        --arg phase ${shellQuote suite.phaseId} \
        --arg suite ${shellQuote suite.suiteName} \
        --arg name ${shellQuote suite.label} \
        '{type:"suite_definition",phase:$phase,suite:$suite,name:$name}'
    '';

  renderPhaseCase =
    suite:
    ''
      ${shellQuote suite.suiteName})
        shift
        "$0" jobs ${suite.taskId} "''${phenix_ci_verbose_arg[@]}" "$@"
        ;;
    '';

  renderPhaseRun =
    suite:
    ''
      if "$0" jobs ${suite.taskId} "''${phenix_ci_verbose_arg[@]}"; then
        :
      else
        phenix_ci_failures+=(${shellQuote suite.suiteName})
      fi
    '';

  renderPhaseExec =
    phase:
    let
      suites = suitesForPhase phase.id;
      cases = concatStringsSep "\n" (map renderPhaseCase suites);
      runs = concatStringsSep "\n" (map renderPhaseRun suites);
      listRows = concatStringsSep "\n" (map renderListRow suites);
    in
    ''
      phenix_ci_verbose_arg=()
      if [[ "''${1:-}" == "--verbose" ]]; then
        phenix_ci_verbose_arg=(--verbose)
        shift
      fi

      if [[ "''${1:-}" == "--list" ]]; then
        ${listRows}
        return 0
      fi

      if (( $# > 0 )); then
        case "$1" in
          ${cases}
          *)
            jq -cn \
              --arg phase ${shellQuote phase.id} \
              --arg suite "$1" \
              '{type:"error",kind:"unknown_suite",phase:$phase,suite:$suite}'
            return 2
            ;;
        esac
        return $?
      fi

      phenix_ci_failures=()
      ${runs}

      if (( ''${#phenix_ci_failures[@]} > 0 )); then
        return 1
      fi
    '';

  phaseCommands = listToAttrs (map (
    phase:
    let
      suites = suitesForPhase phase.id;
    in
    {
      name = phase.id;
      value = {
        inherit (phase) description;
        dependencies = map (suite: [ "jobs" suite.taskId ]) suites;
        runtimeInputs = pkgs: [ pkgs.jq ];
        exec = renderPhaseExec phase;
      };
    }
  ) normalizedPhases);

  renderPipelineRun =
    phase:
    ''
      if "$0" ${phase.id}; then
        :
      else
        phenix_ci_failed_phases+=(${shellQuote phase.id})
      fi
    '';

  pipelineRuns = concatStringsSep "\n" (map renderPipelineRun normalizedPhases);
  suiteCount = length suitesWithNeeds;

  pipelineExec = ''
    phenix_ci_failed_phases=()

    ${pipelineRuns}

    if (( ''${#phenix_ci_failed_phases[@]} > 0 )); then
      phenix_ci_failed_json="$(
        printf '%s\n' "''${phenix_ci_failed_phases[@]}" |
          jq -R . |
          jq -sc .
      )"
      jq -cn \
        --argjson suites ${toString suiteCount} \
        --argjson failed_phases "$phenix_ci_failed_json" \
        '{type:"summary",status:"fail",suites:$suites,failed_phases:$failed_phases}'
      return 1
    fi

    jq -cn \
      --argjson suites ${toString suiteCount} \
      '{type:"summary",status:"pass",suites:$suites}'
  '';
in
assert ciValid;
assert needsValid;
assert acyclic;
if normalizedPhases == [ ] then
  fail "mkCi requires at least one suite"
else
  phaseCommands
  // {
    jobs = {
      description = "Run one semantic CI suite";
      commands = suiteCommands;
    };

    pipeline = {
      description = "Run every semantic CI phase locally and report all results";
      dependencies = map (phase: [ phase.id ]) normalizedPhases;
      runtimeInputs = pkgs: [ pkgs.jq ];
      exec = pipelineExec;
    };

    ci-plan = {
      description = "List semantic CI suites and dependencies";
      runtimeInputs = pkgs: [ pkgs.jq ];
      exec = concatStringsSep "\n" (map (
        suite:
        ''
          jq -cn \
            --arg phase ${shellQuote suite.phaseId} \
            --arg suite ${shellQuote suite.suiteName} \
            --arg name ${shellQuote suite.label} \
            --argjson needs ${shellQuote (builtins.toJSON (map (need: need.ref) suite.needs))} \
            '{type:"suite_definition",phase:$phase,suite:$suite,name:$name,needs:$needs}'
        ''
      ) suitesWithNeeds);
    };
  }
