{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/e7a3ca8092b61ff85b6a45bf863ea2b2d6a661b3";
    crane.url = "github:ipetkov/crane/79fd63528501446749c09f08cad486a58c7d173e";
    crane.inputs.nixpkgs.follows = "nixpkgs";
    phenix-flake-ci.url = "path:../..";
  };
  outputs = { nixpkgs, crane, phenix-flake-ci, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      suite = phenix-flake-ci.lib.mkCargoDependencyChecks {
        inherit pkgs;
        craneLib = crane.mkLib pkgs;
        src = ../cargo-test-consumer;
        lockFile = ../cargo-test-consumer/Cargo.lock;
        groups = {
          root.packages = [ "generic-cargo-test-fixture" ];
          child.packages = [ "generic-cargo-child" ];
        };
      };
    in {
      checks.${system} = suite.checks;
      packages.${system} = suite.packages;
    };
}
