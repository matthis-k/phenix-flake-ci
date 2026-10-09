let
  renderGithubWorkflow = import ./render-github-workflow.nix;

  jobs = [
    {
      id = "cached";
      name = "Cached";
      runner = "ubuntu-latest";
      timeout = 10;
      needs = [ ];
      env = { };
      cache = null;
      commands = [
        {
          id = "cached";
          path = [ "cached" ];
          name = "Cached";
        }
      ];
    }
    {
      id = "plain";
      name = "Plain";
      runner = "ubuntu-latest";
      timeout = 10;
      needs = [ ];
      env = { };
      cache = null;
      commands = [
        {
          id = "plain";
          path = [ "plain" ];
          name = "Plain";
        }
      ];
    }
  ];

  workflow = renderGithubWorkflow {
    inherit jobs;
    clean = false;
    nixCache = {
      enable = true;
      jobs = [ "cached" ];
      primaryKey = "nix-\${{ runner.os }}-\${{ github.job }}-fixture";
      restorePrefixesFirstMatch = [ "nix-\${{ runner.os }}-\${{ github.job }}-" ];
      gcMaxStoreSizeLinux = "2G";
    };
  };
  oneLine = builtins.replaceStrings [ "\n" ] [ " " ] workflow;

  scopedWorkflow = renderGithubWorkflow {
    jobs = builtins.map (job: if job.id == "cached" then job // { impact = { kind = "cargo"; packages = [ "crate-a" ]; }; } else job) jobs;
    clean = false;
    mainBranch = "release";
    pullRequestJobs = [ "cached" ];
    prImpact = {
      enable = true;
      workspace = "rust";
    };
    nixCache = {
      enable = true;
      jobs = [ "cached" ];
      primaryKey = "nix-fixture";
      saveOnDefaultBranch = true;
    };
  };
  scopedOneLine = builtins.replaceStrings [ "\n" ] [ " " ] scopedWorkflow;
  unknownPullRequestJob = builtins.tryEval (builtins.deepSeq (renderGithubWorkflow {
    inherit jobs;
    pullRequestJobs = [ "missing" ];
  }) true);
  missingPullRequestDependency = builtins.tryEval (builtins.deepSeq (renderGithubWorkflow {
    jobs = builtins.map (job: if job.id == "plain" then job // { needs = [ "cached" ]; } else job) jobs;
    pullRequestJobs = [ "plain" ];
  }) true);
  invalidNixSavePolicy = builtins.tryEval (builtins.deepSeq (renderGithubWorkflow {
    inherit jobs;
    nixCache = {
      enable = true;
      jobs = [ "cached" ];
      primaryKey = "nix-fixture";
      saveOnDefaultBranch = "yes";
    };
  }) true);

  invalidJob = builtins.tryEval (
    builtins.deepSeq (renderGithubWorkflow {
      inherit jobs;
      clean = false;
      nixCache = {
        enable = true;
        jobs = [ "missing" ];
        primaryKey = "fixture";
      };
    }) true
  );
in
{
  githubNixCacheIsOptInAndPinned =
    assert builtins.match ".*nix-community/cache-nix-action@7df957e333c1e5da7721f60227dbba6d06080569.*" oneLine != null;
    assert builtins.match ".*primary-key:.*nix-.*runner.os.*github.job.*fixture.*" oneLine != null;
    assert builtins.match ".*restore-prefixes-first-match:.*nix-.*runner.os.*github.job.*" oneLine != null;
    assert builtins.match ".*gc-max-store-size-linux:.*2G.*" oneLine != null;
    true;

  githubNixCacheSavesOnlyOnDefaultBranch =
    assert builtins.match ".*save:.*github.ref == 'refs/heads/release'.*" scopedOneLine != null;
    assert builtins.match ".*if: github.event_name != 'pull_request'.*" scopedOneLine != null;
    assert builtins.match ".*github.event_name.*skipped.*" scopedOneLine != null;
    assert builtins.match ".*name: Dependency impact plan.*" scopedOneLine != null;
    assert builtins.match ".*fetch-depth: 0.*" scopedOneLine != null;
    assert builtins.match ".*needs.impact.outputs.jobs.*cached.*" scopedOneLine != null;
    assert builtins.match ".*impact.*cached.*fromJSON.*" scopedOneLine != null;
    true;

  githubPullRequestSelectionValidatesDependencyClosure =
    assert !unknownPullRequestJob.success;
    assert !missingPullRequestDependency.success;
    assert !invalidNixSavePolicy.success;
    true;

  githubNixCacheRejectsUnknownJobs =
    assert !invalidJob.success;
    true;
}
