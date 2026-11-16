{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/e7a3ca8092b61ff85b6a45bf863ea2b2d6a661b3";
    crane.url = "github:ipetkov/crane/79fd63528501446749c09f08cad486a58c7d173e";
    phenix-flake-ci.url = "path:../..";
  };
  outputs = { nixpkgs, crane, phenix-flake-ci, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      inherit (pkgs) lib;
      group = member: {
          packages = [ "cargo-deps-${member}" ];
          # Real selected members overlay the complete dummy workspace.
          # Original full member list and Cargo.lock remain unchanged.
          src = lib.fileset.toSource {
            root = ./workspace;
            fileset = lib.fileset.unions [
              ./workspace/Cargo.toml
              ./workspace/Cargo.lock
              (./workspace + "/${member}")
            ];
          };
        };
      suite = phenix-flake-ci.lib.mkCargoDependencyChecks {
        inherit pkgs;
        craneLib = crane.mkLib pkgs;
        src = ./workspace;
        lockFile = ./workspace/Cargo.lock;
        groups = {
          a = group "a";
          b = group "b";
        };
      };
    in {
      checks.${system} = suite.checks;
      packages.${system} = suite.packages;
    };
}
