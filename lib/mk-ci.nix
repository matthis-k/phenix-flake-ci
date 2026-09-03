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
      fail "mkCi ci.stepName is phase-owned"
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

  renderSuiteFunction =
    suite:
    let
      label = shellQuote suite.label;
      success = "printf 'PASS %s\\n' ${label}";
      failure = "printf 'FAIL %s\\n' ${label} >&2";
    in
    if suite.quiet then
      ''
        ${suite.functionId}() {
          local phenix_ci_log phenix_ci_status

          if [[ "''${PHENIX_CI_VERBOSE:-0}" == "1" ]]; then
            if (
              ${suite.exec}
            ); then
              ${success}
              return 0
            else
              phenix_ci_status=$?
              ${failure}
              return "$phenix_ci_status"
            fi
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
            ${failure}
            cat "$phenix_ci_log" >&2
            rm -f "$phenix_ci_log"
            return "$phenix_ci_status"
          fi
        }
      ''
    else
      ''
        ${suite.functionId}() {
          local phenix_ci_status
          if (
            ${suite.exec}
          ); then
            ${success}
            return 0
          else
            phenix_ci_status=$?
            ${failure}
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
      phenix_ci_failures+=(${shellQuote suite.label})
    fi
  '';

  renderPhaseExec =
    phase: suites:
    let
      functions = concatStringsSep "\n" (map renderSuiteFunction suites);
      cases = concatStringsSep "\n" (map renderSuiteCase suites);
      runs = concatStringsSep "\n" (map renderSuiteRun suites);
      listRows = concatStringsSep "\n" (
        map (suite: "printf '%-24s %s\\n' ${shellQuote suite.suiteName} ${shellQuote suite.label}") suites
      );
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
            printf 'Unknown ${phase.id} suite: %s\\n' "$1" >&2
            return 2
            ;;
        esac
        return $?
      fi

      phenix_ci_failures=()
      ${runs}

      if (( ''${#phenix_ci_failures[@]} > 0 )); then
        printf '\\nFAILED %d suite(s):\\n' "''${#phenix_ci_failures[@]}" >&2
        printf '  %s\\n' "''${phenix_ci_failures[@]}" >&2
        return 1
      fi
    '';

  runtimeInputsFor =
    suites: pkgs:
    [ pkgs.coreutils ]
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
                command = {
                  inherit (phase) description;
                  runtimeInputs = runtimeInputsFor suites;
                  exec = renderPhaseExec phase suites;
                  ci = sharedCi // {
                    stepName = phase.name;
                  };
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

  aliases = builtins.listToAttrs (
    map (phase: {
      name = phase.id;
      value = {
        inherit (phase) description;
        dependencies = [
          [
            "pipeline"
            phase.id
          ]
        ];
        exec = ''
          "$0" pipeline ${phase.id} "$@"
        '';
      };
    }) normalizedPhases
  );
in
if normalizedPhases == [ ] then
  fail "mkCi requires at least one suite"
else
  aliases
  // {
    pipeline = {
      description = "Run semantic CI phases";
      order = map (phase: phase.id) normalizedPhases;
      commands = phaseCommands;
    };
  }
