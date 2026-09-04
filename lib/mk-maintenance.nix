{
  ciSchemaVersion,
  normalizeGitHooks,
  renderMaintenance,
  renderGithubWorkflow,
}:
{
  name ? "maintenance",
  description ? "Repository maintenance commands",
  commands,
  ci ? { },
  gitHooks ? { },
}:
let
  reservedCommands = builtins.filter (command: builtins.hasAttr command commands) [
    "index"
    "invoke"
  ];
  commandsValid =
    if reservedCommands == [ ] then
      true
    else
      throw "phenix-flake-ci: reserved top-level commands: ${builtins.concatStringsSep ", " reservedCommands}";

  graph = import ./maintenance-command-graph.nix {
    maintenance = { inherit name commands; };
  };

  rendered = renderMaintenance {
    inherit name description commands;
  };

  normalizedGitHooks = normalizeGitHooks {
    inherit name commands gitHooks;
  };

  index = import ./maintenance-index.nix {
    inherit
      name
      description
      commands
      ciSchemaVersion
      ;
    jobs = rendered.publicJobs;
    inherit (rendered) matrix;
    gitHooks = normalizedGitHooks;
  };

  github = ci.github or { };
  githubEnabled = github.enable or false;
  githubWorkflow =
    if githubEnabled then
      renderGithubWorkflow {
        inherit (rendered) jobs;
        outputName = github.outputName or "phenix-maintenance";
        workflowName = github.workflowName or "CI";
        mainBranch = github.mainBranch or "main";
        gateName = github.gateName or "Maintenance checks";
        clean = github.clean or true;
      }
    else
      null;
in
assert commandsValid;
assert graph.valid;
{
  inherit
    name
    description
    commands
    index
    ;
  inherit (rendered) script;
  gitHooks = normalizedGitHooks;
  ci = {
    schemaVersion = ciSchemaVersion;
    jobCount = builtins.length rendered.publicJobs;
    jobs = rendered.publicJobs;
    inherit (rendered) matrix;
  }
  // (
    if githubEnabled then
      {
        github = {
          workflow = githubWorkflow;
          outputName = github.outputName or "phenix-maintenance";
        };
      }
    else
      { }
  );
}
