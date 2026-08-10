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
  rendered = renderMaintenance {
    inherit name description commands;
  };

  normalizedGitHooks = normalizeGitHooks {
    inherit name commands gitHooks;
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
{
  inherit
    name
    description
    commands
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
