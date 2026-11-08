{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/e7a3ca8092b61ff85b6a45bf863ea2b2d6a661b3";
    phenix-flake-ci.url = "path:../..";
  };
  outputs = { nixpkgs, phenix-flake-ci, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      automatic = phenix-flake-ci.lib.mkCargoTestChecks {
        inherit pkgs;
        src = ./.;
        lockFile = ./Cargo.lock;
        noDefaultFeatures = true;
      };
      cargo = phenix-flake-ci.lib.mkCargoTestChecks {
        inherit pkgs;
        src = ./.;
        lockFile = ./Cargo.lock;
        groups = {
          root = {
            packages = [ "generic-cargo-test-fixture" ];
            targets.lib = true;
          };
          child = {
            packages = [ "generic-cargo-child" ];
            targets.lib = true;
          };
          integration = {
            packages = [ "generic-cargo-test-fixture" ];
            targets.tests = [ "basic_integration" ];
          };
          "no-default" = {
            profile = "no-default";
          };
        };
      };
    in {
      checks.${system} = cargo.checks // automatic.checks;
      packages.${system} = {
        default = phenix-flake-ci.lib.mkCargoTestPackage {
          inherit pkgs;
          src = ./.;
          lockFile = ./Cargo.lock;
        };
      } // phenix-flake-ci.lib.mkCargoTestPackages {
        inherit pkgs;
        src = ./.;
        lockFile = ./Cargo.lock;
        exposeCompiled = true;
      } // cargo.packages;
    };
}