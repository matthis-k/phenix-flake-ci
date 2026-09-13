let
  ciSchemaVersion = 3;
  mkCi = import ./mk-ci.nix;
  renderMaintenance = import ./render-maintenance-with-cache.nix;
  renderGithubWorkflow = import ./render-github-workflow.nix;
  normalizeGitHooks = import ./normalize-git-hooks.nix;
  maintenanceCommandGraph = import ./maintenance-command-graph.nix;
  scopeOutputName = import ./scope-output-name.nix;
  mkMaintenance = import ./mk-maintenance.nix {
    inherit
      ciSchemaVersion
      normalizeGitHooks
      renderMaintenance
      renderGithubWorkflow
      ;
  };
  mkMaintenancePackage = import ./mk-maintenance-package.nix;

  mkMaintenanceOutputs =
    {
      maintenance,
      systems,
      pkgsFor,
      outputName ? "phenix-maintenance",
    }:
    let
      graph = maintenanceCommandGraph { inherit maintenance; };
      scopePaths = graph.commandPaths;

      materialized =
        system:
        let
          root = mkMaintenancePackage {
            pkgs = pkgsFor system;
            inherit maintenance outputName;
          };
          scoped = builtins.map (
            path: {
              inherit path;
              value = mkMaintenancePackage {
                pkgs = pkgsFor system;
                inherit maintenance outputName;
                commandPath = path;
              };
            }
          ) scopePaths;
        in
        { inherit root scoped; };

      perSystem =
        {
          selector,
          includeGitHooks ? false,
        }:
        builtins.listToAttrs (
          builtins.map (
            system:
            let
              value = materialized system;
              scopedOutputs = builtins.listToAttrs (
                builtins.map (entry: {
                  name = scopeOutputName {
                    inherit outputName;
                    path = entry.path;
                  };
                  value = selector entry.value;
                }) value.scoped
              );
              hookOutputs =
                if includeGitHooks && value.root.gitHooksPackage != null then
                  {
                    "${outputName}-git-hooks" = value.root.gitHooksPackage;
                  }
                else
                  { };
            in
            {
              name = system;
              value = {
                ${outputName} = selector value.root;
              }
              // scopedOutputs
              // hookOutputs;
            }
          ) systems
        );
    in
    {
      packages = perSystem {
        selector = value: value.package;
        includeGitHooks = true;
      };
      apps = perSystem {
        selector = value: value.app;
      };
    };
in
{
  version = "0.12.0";
  tests = import ./tests.nix;
  inherit
    ciSchemaVersion
    maintenanceCommandGraph
    mkCi
    mkMaintenance
    mkMaintenanceOutputs
    mkMaintenancePackage
    renderGithubWorkflow
    renderMaintenance
    scopeOutputName
    ;
}
