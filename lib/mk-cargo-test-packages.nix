# Discover Cargo workspace members and expose one runnable test.* package
# per crate. All wrappers reference the SAME compiled test corpus, so Nix
# schedules compilation once and test jobs run only already-built binaries.
{
  pkgs,
  src,
  lockFile,
  compiledName ? "cargo-workspace-tests",
  exposeCompiled ? false,
  cargoArgs ? [ "--workspace" "--all-targets" ],
}:
let
  inherit (pkgs) lib;
  uniqueNames = import ./cargo-workspace-members.nix { inherit lib src; };
  compiled = import ./mk-cargo-test-package.nix {
    inherit pkgs src lockFile cargoArgs;
    name = compiledName;
  };
  wrappers = builtins.listToAttrs (map (package: {
    name = "test.cargo-${package}";
    value = pkgs.writeShellApplication {
      name = "cargo-test-${package}";
      text = ''
        exec ${compiled}/bin/${compiledName} --package ${lib.escapeShellArg package} "$@"
      '';
    };
  }) uniqueNames);
in
wrappers // lib.optionalAttrs exposeCompiled {
    "${compiledName}" = compiled;
  }
