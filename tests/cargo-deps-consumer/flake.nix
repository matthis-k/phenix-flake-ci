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
      group = member:
        let
          manifest = (pkgs.formats.toml { }).generate "Cargo.toml" {
            workspace = {
              members = [ member ];
              resolver = "2";
            };
          };
        in {
          packages = [ "cargo-deps-${member}" ];
          # Include only this group and its workspace manifest/lock. Changes
          # to the other group cannot invalidate this check's source path.
          src = lib.fileset.toSource {
            root = ./workspace;
            fileset = lib.fileset.unions [
              ./workspace/Cargo.toml
              ./workspace/Cargo.lock
              (./workspace + "/${member}")
            ];
          };
          postUnpack = ''cp ${manifest} "$sourceRoot/Cargo.toml"'';
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
