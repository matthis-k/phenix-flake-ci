let
  mkGraph = commands:
    import ./maintenance-command-graph.nix {
      maintenance = {
        name = "maintenance";
        inherit commands;
      };
      pkgs = { };
    };

  mkCi = import ./mk-ci.nix;

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

  semanticCommands = mkCi {
    build.compile = {
      name = "Rust build";
      exec = "cargo build --quiet";
      runtimeInputs = pkgs: [ pkgs.cargo ];
    };
    test.rust = {
      name = "Rust tests";
      exec = "cargo test --quiet";
      runtimeInputs = pkgs: [ pkgs.cargo ];
    };
    runtime.startup = {
      name = "CLI startup";
      exec = "phenix --version";
    };
    ci.timeoutMinutes = 60;
  };
  semanticTest = semanticCommands.phases.commands.test;
  semanticTestScript = builtins.replaceStrings [ "\n" ] [ " " ] semanticTest.exec;
  semanticTestInputs = semanticTest.runtimeInputs {
    coreutils = "coreutils";
    jq = "jq";
    cargo = "cargo";
  };
  semanticPipeline = semanticCommands.pipeline;
  semanticPipelineScript = builtins.replaceStrings [ "\n" ] [ " " ] semanticPipeline.exec;
  semanticPipelineInputs = semanticPipeline.runtimeInputs { jq = "jq"; };
  semanticRendered = import ./render-maintenance.nix {
    name = "maintenance";
    commands = semanticCommands;
  };
  semanticJob = builtins.head semanticRendered.jobs;

  invalidQuietResult = builtins.tryEval (
    builtins.deepSeq (mkCi {
      test.bad = {
        quiet = "yes";
        exec = "true";
      };
    }) true
  );
  invalidCiOwnershipResult = builtins.tryEval (
    builtins.deepSeq (mkCi {
      test.good.exec = "true";
      ci.stepName = "Other";
    }) true
  );

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

  semanticPipelineIsOrdered =
    assert semanticCommands.phases.order == [
      "build"
      "test"
      "runtime"
    ];
    assert semanticPipeline.dependencies == [
      [
        "phases"
        "build"
      ]
      [
        "phases"
        "test"
      ]
      [
        "phases"
        "runtime"
      ]
    ];
    assert semanticPipeline.ci.stage == "ci";
    assert semanticPipeline.ci.timeoutMinutes == 60;
    true;

  semanticPipelineProjectsToOneJobAndOneStep =
    assert builtins.length semanticRendered.jobs == 1;
    assert semanticJob.id == "ci";
    assert builtins.map (command: command.path) semanticJob.commands == [ [ "pipeline" ] ];
    true;

  semanticPhasesHaveInteractiveAliases =
    assert semanticCommands.test.dependencies == [
      [
        "phases"
        "test"
      ]
    ];
    assert builtins.match ".*phases test.*" semanticCommands.test.exec != null;
    true;

  semanticSuitesEmitJsonAndHideSuccessOutput =
    assert builtins.match ".*type:\"suite\".*status:\"pass\".*" semanticTestScript != null;
    assert builtins.match ".*type:\"suite\".*status:\"fail\".*" semanticTestScript != null;
    assert builtins.match ".*--rawfile output.*" semanticTestScript != null;
    assert builtins.match ".*PHENIX_CI_VERBOSE.*" semanticTestScript != null;
    assert builtins.match ".*cargo test --quiet.*" semanticTestScript != null;
    true;

  semanticPipelineCollectsAllPhaseFailures =
    assert builtins.match ".*phases build.*phases test.*phases runtime.*" semanticPipelineScript != null;
    assert builtins.match ".*failed_phases.*status:\"fail\".*" semanticPipelineScript != null;
    true;

  semanticPhaseRuntimeInputsIncludeJsonReporter =
    assert semanticTestInputs == [
      "coreutils"
      "jq"
      "cargo"
    ];
    assert semanticPipelineInputs == [ "jq" ];
    true;

  invalidSuiteQuietRejected =
    assert !invalidQuietResult.success;
    true;

  invalidCiOwnershipRejected =
    assert !invalidCiOwnershipResult.success;
    true;

  workflowUsesScopedOutput =
    assert builtins.match ".*nix run --quiet \\.#${fixOutput} -- fix.*" workflowOneLine != null;
    true;
}
