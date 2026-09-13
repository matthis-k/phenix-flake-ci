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

  githubNixCacheRejectsUnknownJobs =
    assert !invalidJob.success;
    true;
}
