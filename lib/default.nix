let
  ciSchemaVersion = 2;
  renderMaintenance = import ./render-maintenance.nix;
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
            inherit maintenance;
          };
          scoped = builtins.map (
            path: {
              inherit path;
              value = mkMaintenancePackage {
                pkgs = pkgsFor system;
                inherit maintenance;
                commandPath = path;
              };
            }
          ) scopePaths;
        in
        { inherit root scoped; };

      perSystem =
        selector:
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
            in
            {
              name = system;
              value = {
                ${outputName} = selector value.root;
              } // scopedOutputs;
            }
          ) systems
        );
    in
    {
      packages = perSystem (value: value.package);
      apps = perSystem (value: value.app);
    };
in
{
  version = "0.7.0";
  tests = import ./tests.nix;
  inherit
    ciSchemaVersion
    maintenanceCommandGraph
    mkMaintenance
    mkMaintenanceOutputs
    mkMaintenancePackage
    renderGithubWorkflow
    renderMaintenance
    scopeOutputName
    ;
}
