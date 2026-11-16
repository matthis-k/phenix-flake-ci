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
      group =
        member:
        let
          filtered = lib.fileset.toSource {
            root = ./workspace;
            fileset = lib.fileset.unions [
              ./workspace/Cargo.toml
              ./workspace/Cargo.lock
              (./workspace + "/${member}")
            ];
          };
          # Adversarial case: overlay b supplies the *wrong* workspace root
          # manifest and lockfile. The library must restore Crane's complete
          # workspace identity before Cargo --locked starts.
          narrowed = (pkgs.formats.toml { }).generate "Cargo.toml" {
            workspace = {
              members = [ member ];
              resolver = "2";
            };
          };
          adversarial = pkgs.runCommand "adversarial-cargo-group-${member}" { } ''
            mkdir -p "$out"
            cp -r --no-preserve=mode ${filtered}/. "$out/"
            chmod -R u+w "$out"
            cp ${narrowed} "$out/Cargo.toml"
            printf '%s\\n' 'invalid package lockfile' > "$out/Cargo.lock"
          '';
        in
        {
          packages = [ "cargo-deps-${member}" ];
          # Member a is the normal filtered case; b tests hostile root
          # metadata. Neither may change unrelated member compilation.
          src = if member == "b" then adversarial else filtered;
        };
      suite = phenix-flake-ci.lib.mkCargoDependencyChecks {
        inherit pkgs;
        craneLib = crane.mkLib pkgs;
        src = ./workspace;
        lockFile = ./workspace/Cargo.lock;
        groups = {
          a = group "a";
          b = group "b";
          # A separate doctest check shares the *same* default-feature
          # dependency corpus; it must not spawn a third prebuild profile.
          "b-doc" = (group "b") // { targets = [ "--doc" ]; };
        };
      };
    in {
      checks.${system} = suite.checks;
      packages.${system} = suite.packages;
    };
}
