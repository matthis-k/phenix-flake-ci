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
      commands = phenix-flake-ci.lib.mkCi {
        build.compile = {
          name = "Rust build";
          exec = ''
            printf 'hidden build noise\n'
          '';
        };
        test = {
          a-failure = {
            name = "Failing suite";
            exec = ''
              printf 'expected failure detail\n' >&2
              exit 7
            '';
          };
          b-success = {
            name = "Passing suite";
            exec = ''
              printf 'hidden success noise\n'
            '';
          };
        };
        runtime.startup = {
          name = "Runtime startup";
          exec = ''
            printf 'hidden runtime noise\n'
          '';
        };
      };
      maintenance = phenix-flake-ci.lib.mkMaintenance {
        name = "maintenance";
        inherit commands;
      };
      outputs = phenix-flake-ci.lib.mkMaintenanceOutputs {
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
    in
    {
      apps.${system} = {
        build = outputs.apps.${system}.${scopedOutput [ "build" ]};
        test = outputs.apps.${system}.${scopedOutput [ "test" ]};
        runtime = outputs.apps.${system}.${scopedOutput [ "runtime" ]};
        pipeline = outputs.apps.${system}.${scopedOutput [ "pipeline" ]};
      };
    };
}
