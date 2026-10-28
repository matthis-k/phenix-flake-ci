{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/e7a3ca8092b61ff85b6a45bf863ea2b2d6a661b3";
    phenix-flake-ci.url = "path:../..";
  };
  outputs = { nixpkgs, phenix-flake-ci, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
    in {
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
      };
    };
}
