let
  mkGraph = commands:
    import ./maintenance-command-graph.nix {
      maintenance = {
        name = "maintenance";
        inherit commands;
      };
      pkgs = { };
    };

  commands = {
    leaf = {
      runtimeInputs = [ "leaf-tool" ];
      exec = "printf 'leaf-body\\n'";
    };

    sibling = {
      runtimeInputs = [ "sibling-tool" ];
      exec = "printf 'sibling-body\\n'";
    };

    group = {
      runtimeInputs = [ "group-tool" ];
      commands.child = {
        runtimeInputs = [ "child-tool" "group-tool" ];
        exec = "printf 'child-body\\n'";
      };
    };

    dependent = {
      runtimeInputs = [ "dependent-tool" ];
      dependencies = [ [ "leaf" ] ];
      exec = ''
        "$0" leaf
        printf 'dependent-body\n'
      '';
    };
  };

  maintenance = {
    name = "maintenance";
    inherit commands;
  };
  graph = mkGraph commands;
  leafInputs = graph.runtimeInputsForPath [ "leaf" ];
  groupInputs = graph.runtimeInputsForPath [ "group" ];
  dependentInputs = graph.runtimeInputsForPath [ "dependent" ];

  scopedDependentScript = import ./render-scoped-maintenance.nix {
    inherit maintenance;
    paths = graph.pathsForPath [ "dependent" ];
  };
  scopedOneLine = builtins.replaceStrings [ "\n" ] [ " " ] scopedDependentScript;

  cycleResult = builtins.tryEval (builtins.deepSeq (mkGraph {
    first = {
      dependencies = [ [ "second" ] ];
      exec = "true";
    };
    second = {
      dependencies = [ [ "first" ] ];
      exec = "true";
    };
  }) true);

  missingResult = builtins.tryEval (builtins.deepSeq (mkGraph {
    broken = {
      dependencies = [ [ "missing" ] ];
      exec = "true";
    };
  }) true);

  scopeOutputName = import ./scope-output-name.nix;
  outputName = "phenix-maintenance";
  fixOutput = scopeOutputName {
    inherit outputName;
    path = [ "fix" ];
  };
  workflow = import ./render-github-workflow.nix {
    inherit outputName;
    clean = false;
    jobs = [
      {
        id = "fix";
        name = "Fix";
        runner = "ubuntu-latest";
        timeout = 10;
        needs = [ ];
        env = { };
        commands = [
          {
            id = "fix";
            path = [ "fix" ];
            name = "Fix";
          }
        ];
      }
    ];
  };
  workflowOneLine = builtins.replaceStrings [ "\n" ] [ " " ] workflow;
in
{
  leafClosureIsScoped =
    assert leafInputs == [ "leaf-tool" ];
    assert !(builtins.elem "sibling-tool" leafInputs);
    true;

  aggregateIncludesChildren =
    assert groupInputs == [ "group-tool" "child-tool" ];
    true;

  explicitDependencyIncluded =
    assert dependentInputs == [ "dependent-tool" "leaf-tool" ];
    true;

  scopedScriptExcludesSiblingBodies =
    assert builtins.match ".*dependent-body.*" scopedOneLine != null;
    assert builtins.match ".*leaf-body.*" scopedOneLine != null;
    assert builtins.match ".*sibling-body.*" scopedOneLine == null;
    true;

  dependencyCycleRejected =
    assert !cycleResult.success;
    true;

  missingDependencyRejected =
    assert !missingResult.success;
    true;

  workflowUsesScopedOutput =
    assert builtins.match ".*nix run \\.#${fixOutput} -- fix.*" workflowOneLine != null;
    true;
}
