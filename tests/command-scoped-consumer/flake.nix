{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/e7a3ca8092b61ff85b6a45bf863ea2b2d6a661b3";
    phenix-flake-ci.url = "path:../..";
  };

  outputs =
    {
      nixpkgs,
      phenix-flake-ci,
      ...
    }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      fixTool = pkgs.writeShellScriptBin "fixture-fix-tool" ''
        printf 'fix\n'
      '';
      unrelatedTool = pkgs.writeShellScriptBin "fixture-unrelated-tool" ''
        printf 'unrelated\n'
      '';
      maintenance = phenix-flake-ci.lib.mkMaintenance {
        name = "maintenance";
        commands = {
          fix = {
            runtimeInputs = [ fixTool ];
            exec = "fixture-fix-tool";
          };
          check = {
            runtimeInputs = [ unrelatedTool ];
            exec = "fixture-unrelated-tool";
          };
          aggregate.commands = {
            first = {
              runtimeInputs = [ fixTool ];
              exec = "fixture-fix-tool";
            };
            second.exec = "printf 'second\\n'";
          };
          dependent = {
            dependencies = [ [ "fix" ] ];
            exec = ''
              "$0" fix
              printf 'dependent\n'
            '';
          };
        };
      };
      maintenanceOutputs = phenix-flake-ci.lib.mkMaintenanceOutputs {
        inherit maintenance;
        systems = [ system ];
        pkgsFor = _: pkgs;
        outputName = "maintenance";
      };
      scopedOutput = path:
        phenix-flake-ci.lib.scopeOutputName {
          outputName = "maintenance";
          inherit path;
        };
      fixOutput = scopedOutput [ "fix" ];
      aggregateOutput = scopedOutput [ "aggregate" ];
      dependentOutput = scopedOutput [ "dependent" ];
    in
    {
      packages.${system} = {
        fix = maintenanceOutputs.packages.${system}.${fixOutput};
        dispatcher = maintenanceOutputs.packages.${system}.maintenance;
        unrelated-tool = unrelatedTool;
      };
      apps.${system} = {
        fix = maintenanceOutputs.apps.${system}.${fixOutput};
        aggregate = maintenanceOutputs.apps.${system}.${aggregateOutput};
        dependent = maintenanceOutputs.apps.${system}.${dependentOutput};
        dispatcher = maintenanceOutputs.apps.${system}.maintenance;
      };
    };
}
