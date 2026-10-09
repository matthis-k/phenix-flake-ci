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
      needs = [ ];
      cache = false;
      exec = "phenix --version";
    };
    ci = {
      timeoutMinutes = 60;
      cache = {
        paths = [
          "\${{ runner.temp }}/cargo-home"
          "\${{ runner.temp }}/cargo-target"
        ];
        key = "rust-\${{ runner.os }}-\${{ github.sha }}";
        restoreKeys = [ "rust-\${{ runner.os }}-" ];
        writer = "build.compile";
        saveOnDefaultBranch = true;
      };
    };
  };

  semanticBuildJob = semanticCommands.jobs.commands.build-compile;
  semanticTestJob = semanticCommands.jobs.commands.test-rust;
  semanticRuntimeJob = semanticCommands.jobs.commands.runtime-startup;
  semanticTestScript = builtins.replaceStrings [ "\n" ] [ " " ] semanticTestJob.exec;
  semanticTestInputs = semanticTestJob.runtimeInputs {
    coreutils = "coreutils";
    jq = "jq";
    cargo = "cargo";
  };
  semanticPipeline = semanticCommands.pipeline;
  semanticPipelineScript = builtins.replaceStrings [ "\n" ] [ " " ] semanticPipeline.exec;
  semanticPipelineInputs = semanticPipeline.runtimeInputs { jq = "jq"; };
  semanticRendered = import ./render-maintenance-with-cache.nix {
    name = "maintenance";
    commands = semanticCommands;
  };
  renderedJob = id: builtins.head (builtins.filter (job: job.id == id) semanticRendered.jobs);
  renderedBuild = renderedJob "build-compile";
  renderedTest = renderedJob "test-rust";
  renderedRuntime = renderedJob "runtime-startup";

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
  missingSemanticNeed = builtins.tryEval (
    builtins.deepSeq (mkCi {
      build.compile.exec = "true";
      test.good = {
        needs = [ "build.missing" ];
        exec = "true";
      };
    }) true
  );
  cyclicSemanticNeed = builtins.tryEval (
    builtins.deepSeq (mkCi {
      build.first = {
        needs = [ "build.second" ];
        exec = "true";
      };
      build.second = {
        needs = [ "build.first" ];
        exec = "true";
      };
    }) true
  );
  unknownCacheWriter = builtins.tryEval (
    builtins.deepSeq (mkCi {
      test.good.exec = "true";
      ci.cache = {
        paths = [ "/tmp/cache" ];
        key = "fixture";
        writer = "test.missing";
      };
    }) true
  );
  disabledCacheWriter = builtins.tryEval (
    builtins.deepSeq (mkCi {
      test.writer = {
        cache = false;
        exec = "true";
      };
      ci.cache = {
        paths = [ "/tmp/cache" ];
        key = "fixture";
        writer = "test.writer";
      };
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
        cache = {
          paths = [ "\${{ runner.temp }}/cache" ];
          key = "fixture-\${{ github.sha }}";
          restoreKeys = [ "fixture-" ];
        };
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
  ownedCacheWorkflow = import ./render-github-workflow.nix {
    inherit outputName;
    clean = false;
    jobs = [
      renderedBuild
      renderedTest
    ];
  };
  ownedCacheOneLine = builtins.replaceStrings [ "\n" ] [ " " ] ownedCacheWorkflow;
  impactedWorkflow = import ./render-github-workflow.nix {
    inherit outputName;
    clean = false;
    pullRequestJobs = [ "build-compile" ];
    prImpact = {
      enable = true;
      workspace = "rust";
    };
    jobs = [
      (renderedBuild // {
        impact = {
          kind = "cargo";
          packages = null;
        };
      })
    ];
  };
  impactedWorkflowFlat = builtins.replaceStrings [ "\n" ] [ " " ] impactedWorkflow;
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

  semanticPhasesClassifyWithoutOrdering =
    assert semanticCommands.build.dependencies == [ [ "jobs" "build-compile" ] ];
    assert semanticCommands.test.dependencies == [ [ "jobs" "test-rust" ] ];
    assert semanticCommands.runtime.dependencies == [ [ "jobs" "runtime-startup" ] ];
    assert semanticPipeline.dependencies == [
      [ "build" ]
      [ "test" ]
      [ "runtime" ]
    ];
    true;

  semanticJobsFormDependencyDag =
    assert builtins.length semanticRendered.jobs == 3;
    assert renderedBuild.needs == [ ];
    assert renderedTest.needs == [ "build-compile" ];
    assert renderedRuntime.needs == [ ];
    true;

  semanticCacheCanBeSharedOrSkipped =
    assert renderedBuild.cache.key == "rust-\${{ runner.os }}-\${{ github.sha }}";
    assert renderedBuild.cache.save;
    assert renderedBuild.cache.saveOnDefaultBranch;
    assert renderedTest.cache.paths == [
      "\${{ runner.temp }}/cargo-home"
      "\${{ runner.temp }}/cargo-target"
    ];
    assert !renderedTest.cache.save;
    assert renderedRuntime.cache == null;
    true;

  semanticCacheWriterRendersRestoreAndSingleSave =
    assert builtins.match ".*actions/cache/restore@0057852bfaa89a56745cba8c7296529d2fc39830.*" ownedCacheOneLine != null;
    assert builtins.match ".*actions/cache/save@0057852bfaa89a56745cba8c7296529d2fc39830.*" ownedCacheOneLine != null;
    assert builtins.match ".*steps.phenix-cache-restore.outputs.cache-hit.*" ownedCacheOneLine != null;
    assert builtins.match ".*github.ref == 'refs/heads/main'.*" ownedCacheOneLine != null;
    true;

  semanticSuitesEmitJsonAndHideSuccessOutput =
    assert builtins.match ".*type:\"suite\".*status:\"pass\".*" semanticTestScript != null;
    assert builtins.match ".*type:\"suite\".*status:\"fail\".*" semanticTestScript != null;
    assert builtins.match ".*--rawfile output.*" semanticTestScript != null;
    assert builtins.match ".*PHENIX_CI_VERBOSE.*" semanticTestScript != null;
    assert builtins.match ".*cargo test --quiet.*" semanticTestScript != null;
    true;

  semanticPipelineRemainsCompleteLocalDiagnostic =
    assert builtins.match ".*\"\\$0\" build.*\"\\$0\" test.*\"\\$0\" runtime.*" semanticPipelineScript != null;
    assert builtins.match ".*failed_phases.*status:\"fail\".*" semanticPipelineScript != null;
    true;

  semanticSuiteRuntimeInputsAreNarrow =
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

  missingSemanticDependencyRejected =
    assert !missingSemanticNeed.success;
    true;

  semanticDependencyCycleRejected =
    assert !cyclicSemanticNeed.success;
    true;

  unknownSemanticCacheWriterRejected =
    assert !unknownCacheWriter.success;
    true;

  disabledSemanticCacheWriterRejected =
    assert !disabledCacheWriter.success;
    true;

  impactedJobsReceiveSingleCargoMetadataPackageList =
    assert builtins.match ".*steps.select.outputs.packages.*" impactedWorkflowFlat != null;
    assert builtins.match ".*PHENIX_IMPACT_PACKAGES:.*needs.impact.outputs.packages.*" impactedWorkflowFlat != null;
    assert builtins.match ".*steps.select.outputs.jobs.*" impactedWorkflowFlat != null;
    true;

  workflowUsesScopedOutputCacheAndJsonInvocation =
    assert builtins.match ".*actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830.*" workflowOneLine != null;
    assert builtins.match ".*fixture-.*github.sha.*" workflowOneLine != null;
    assert builtins.match ".*nix run --quiet \\.#${fixOutput} -- invoke.*" workflowOneLine != null;
    assert builtins.match ".*github-actions.*" workflowOneLine != null;
    true;
}
