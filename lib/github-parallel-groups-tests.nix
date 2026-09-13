let
  scopeOutputName = import ./scope-output-name.nix;
  renderWorkflow = import ./render-github-workflow.nix;
  outputName = "phenix-maintenance";

  command = id: name: {
    inherit name;
    id = "jobs/${id}";
    path = [
      "jobs"
      id
    ];
  };

  job =
    {
      id,
      name,
      needs ? [ ],
    }:
    {
      inherit
        id
        name
        needs
        ;
      runner = "ubuntu-latest";
      timeout = 30;
      env = { };
      cache = null;
      commands = [ (command id name) ];
    };

  jobs = [
    (job {
      id = "source";
      name = "Source";
    })
    (job {
      id = "product-runtime";
      name = "Runtime journey";
    })
    (job {
      id = "product-lua";
      name = "Lua journey";
    })
  ];

  parallelGroups.product = {
    name = "Product";
    jobPrefix = "product-";
  };

  workflow = renderWorkflow {
    inherit
      jobs
      outputName
      parallelGroups
      ;
    clean = false;
  };
  workflowOneLine = builtins.replaceStrings [ "\n" ] [ " " ] workflow;

  runtimeOutput = scopeOutputName {
    inherit outputName;
    path = [
      "jobs"
      "product-runtime"
    ];
  };
  luaOutput = scopeOutputName {
    inherit outputName;
    path = [
      "jobs"
      "product-lua"
    ];
  };

  conflictingNeeds = builtins.tryEval (
    builtins.deepSeq (renderWorkflow {
      inherit outputName parallelGroups;
      jobs = [
        (job {
          id = "build";
          name = "Build";
        })
        (job {
          id = "product-runtime";
          name = "Runtime journey";
        })
        (job {
          id = "product-lua";
          name = "Lua journey";
          needs = [ "build" ];
        })
      ];
    }) true
  );

  groupedDependencyTarget = builtins.tryEval (
    builtins.deepSeq (renderWorkflow {
      inherit outputName parallelGroups;
      jobs = [
        (job {
          id = "product-runtime";
          name = "Runtime journey";
        })
        (job {
          id = "product-lua";
          name = "Lua journey";
        })
        (job {
          id = "consumer";
          name = "Consumer";
          needs = [ "product-runtime" ];
        })
      ];
    }) true
  );

  generatedIdCollision = builtins.tryEval (
    builtins.deepSeq (renderWorkflow {
      inherit outputName;
      jobs = [
        (job {
          id = "first-a";
          name = "First A";
        })
        (job {
          id = "first-b";
          name = "First B";
        })
        (job {
          id = "second-a";
          name = "Second A";
        })
        (job {
          id = "second-b";
          name = "Second B";
        })
      ];
      parallelGroups = {
        first = {
          jobs = [
            "first-a"
            "first-b"
          ];
        };
        "first-checks" = {
          jobs = [
            "second-a"
            "second-b"
          ];
        };
      };
    }) true
  );
in
{
  githubParallelGroupsUseOneMatrixDefinition =
    assert builtins.match ".*group-product:.*strategy:.*fail-fast: false.*matrix:.*include:.*" workflowOneLine != null;
    assert builtins.match ".*product-runtime.*product-lua.*" workflowOneLine != null;
    true;

  githubParallelGroupsKeepCommandScopedClosures =
    assert builtins.match ".*${runtimeOutput}.*" workflowOneLine != null;
    assert builtins.match ".*${luaOutput}.*" workflowOneLine != null;
    assert builtins.match ".*PHENIX_CI_OUTPUT.*matrix.output.*" workflowOneLine != null;
    true;

  githubParallelGroupsAddStableSummaryCheck =
    assert builtins.match ".*group-product-checks:.*name: \"Product\".*needs:.*group-product.*" workflowOneLine != null;
    assert builtins.match ".*checks:.*needs:.*source.*group-product-checks.*" workflowOneLine != null;
    true;

  githubParallelGroupsRejectDifferentDependencies =
    assert !conflictingNeeds.success;
    true;

  githubParallelGroupsRejectDependencyTargets =
    assert !groupedDependencyTarget.success;
    true;

  githubParallelGroupsRejectGeneratedIdCollisions =
    assert !generatedIdCollision.success;
    true;
}
