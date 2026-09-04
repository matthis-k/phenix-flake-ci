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
    foldl'
    hashString
    isAttrs
    isBool
    isFunction
    isList
    isString
    length
    map
    match
    removeAttrs
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

  ciValid =
    if !isAttrs ci then
      fail "mkCi ci must be an attribute set"
    else if builtins.hasAttr "stepName" ci then
      fail "mkCi ci.stepName is pipeline-owned"
    else if builtins.hasAttr "enable" ci then
      fail "mkCi ci.enable is always true"
    else
      true;

  sharedCi = (removeAttrs ci [ "stage" ]) // {
    enable = true;
    stage = ci.stage or "ci";
    name = ci.name or "CI";
    needs = ci.needs or [ ];
  };

  suiteFunctionId = phaseId: suiteName: "suite_${hashString "sha256" "${phaseId}/${suiteName}"}";

  normalizeSuite =
    phaseId: suiteName: suite:
    let
      description = suite.description or suiteName;
      label = suite.name or description;
      quiet = suite.quiet or true;
      runtimeInputs = suite.runtimeInputs or [ ];
      exec = suite.exec or null;
    in
    if !isAttrs suite then
      fail "mkCi ${phaseId}.${suiteName} must be an attribute set"
    else if match "^[A-Za-z0-9][A-Za-z0-9_-]*$" suiteName == null then
      fail "mkCi ${phaseId} has invalid suite name `${suiteName}`"
    else if !isString description then
      fail "mkCi ${phaseId}.${suiteName}.description must be a string"
    else if !isString label then
      fail "mkCi ${phaseId}.${suiteName}.name must be a string"
    else if !isBool quiet then
      fail "mkCi ${phaseId}.${suiteName}.quiet must be a boolean"
    else if !(isList runtimeInputs || isFunction runtimeInputs) then
      fail "mkCi ${phaseId}.${suiteName}.runtimeInputs must be a list or a pkgs function"
    else if !isString exec then
      fail "mkCi ${phaseId}.${suiteName}.exec must be a shell script string"
    else
      {
        inherit
          suiteName
          description
          label
          quiet
          runtimeInputs
          exec
          ;
        functionId = suiteFunctionId phaseId suiteName;
      };

  normalizeSuites =
    phase:
    if !isAttrs phase.suites then
      fail "mkCi ${phase.id} must be an attribute set of suites"
    else
      map (suiteName: normalizeSuite phase.id suiteName phase.suites.${suiteName}) (attrNames phase.suites);

  renderSuccess =
    phaseId: suite:
    let
      phase = shellQuote phaseId;
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

  renderSuiteFunction =
    phaseId: suite:
    let
      phase = shellQuote phaseId;
      suiteName = shellQuote suite.suiteName;
      label = shellQuote suite.label;
      success = renderSuccess phaseId suite;
    in
    ''
      ${suite.functionId}() {
        local phenix_ci_log phenix_ci_status

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
      }
    '';

  renderSuiteCase = suite: ''
    ${shellQuote suite.suiteName})
      shift
      ${suite.functionId} "$@"
      ;;
  '';

  renderSuiteRun = suite: ''
    if ${suite.functionId}; then
      :
    else
      phenix_ci_failures+=(${shellQuote suite.suiteName})
    fi
  '';

  renderListRow =
    phaseId: suite:
    ''
      jq -cn \
        --arg phase ${shellQuote phaseId} \
        --arg suite ${shellQuote suite.suiteName} \
        --arg name ${shellQuote suite.label} \
        '{type:"suite_definition",phase:$phase,suite:$suite,name:$name}'
    '';

  renderPhaseExec =
    phase: suites:
    let
      functions = concatStringsSep "\n" (map (renderSuiteFunction phase.id) suites);
      cases = concatStringsSep "\n" (map renderSuiteCase suites);
      runs = concatStringsSep "\n" (map renderSuiteRun suites);
      listRows = concatStringsSep "\n" (map (renderListRow phase.id) suites);
    in
    ''
      ${functions}

      if [[ "''${1:-}" == "--verbose" ]]; then
        export PHENIX_CI_VERBOSE=1
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

  runtimeInputsFor =
    suites: pkgs:
    [
      pkgs.coreutils
      pkgs.jq
    ]
    ++ concatLists (
      map (
        suite:
        if isFunction suite.runtimeInputs then suite.runtimeInputs pkgs else suite.runtimeInputs
      ) suites
    );

  normalizedPhases =
    assert ciValid;
    foldl'
      (
        acc: phase:
        let
          suites = normalizeSuites phase;
        in
        if suites == [ ] then
          acc
        else
          acc
          ++ [
            (phase
              // {
                inherit suites;
                command = {
                  inherit (phase) description;
                  runtimeInputs = runtimeInputsFor suites;
                  exec = renderPhaseExec phase suites;
                };
              })
          ]
      )
      [ ]
      phases;

  phaseCommands = builtins.listToAttrs (
    map (phase: {
      name = phase.id;
      value = phase.command;
    }) normalizedPhases
  );

  phaseOrder = map (phase: phase.id) normalizedPhases;

  aliases = builtins.listToAttrs (
    map (phase: {
      name = phase.id;
      value = {
        inherit (phase) description;
        dependencies = [
          [
            "phases"
            phase.id
          ]
        ];
        exec = ''
          "$0" phases ${phase.id} "$@"
        '';
      };
    }) normalizedPhases
  );

  pipelineDependencies = map (phase: [
    "phases"
    phase.id
  ]) normalizedPhases;

  renderPipelineRun = phase: ''
    if "$0" phases ${phase.id}; then
      :
    else
      phenix_ci_failed_phases+=(${shellQuote phase.id})
    fi
  '';

  pipelineRuns = concatStringsSep "\n" (map renderPipelineRun normalizedPhases);
  suiteCount = foldl' (total: phase: total + length phase.suites) 0 normalizedPhases;

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
if normalizedPhases == [ ] then
  fail "mkCi requires at least one suite"
else
  aliases
  // {
    phases = {
      description = "Run one semantic CI phase";
      order = phaseOrder;
      commands = phaseCommands;
    };

    pipeline = {
      description = "Run every semantic CI phase and report all results";
      dependencies = pipelineDependencies;
      runtimeInputs = pkgs: [ pkgs.jq ];
      exec = pipelineExec;
      ci = sharedCi // {
        stepName = "CI";
      };
    };
  }
