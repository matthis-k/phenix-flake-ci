# Thin Rust test generator. The consumer chooses the crate(s) and compilation
# topology; Cargo discovers actual test targets, and existing Flake CI discovers
# the resulting packages."test.*" outputs. No new scheduler or runtime engine.
{
  pkgs,
  src,
  lockFile,
  crate ? null,
  crates ? null,
  strategy ? null,
  cargoArgs ? [ ],
}:
let
  inherit (pkgs) lib;
  fail = message: throw "phenix-flake-ci.mkRustTests: ${message}";
  validName = name:
    builtins.isString name
    && builtins.match "[A-Za-z0-9][A-Za-z0-9_-]*" name != null;
  selected =
    if crate != null then [ crate ]
    else crates;
  selectedValid =
    selected == null
    || (builtins.isList selected
      && selected != [ ]
      && builtins.all validName selected
      && builtins.length selected == builtins.length (lib.unique selected));
  selectedStrategy =
    if strategy != null then strategy
    else if selected == null then "shared"
    else "isolated";
  # Target selectors and --no-run are owned by the generator. Extra flags are
  # for features, flags and profiles; explicit Cargo selectors could silently
  # break the declared package-to-test correspondence.
  reservedArgs = [
    "--workspace" "--package" "-p" "--exclude" "--doc" "--no-run"
    "--all-targets" "--lib" "--bins" "--tests" "--examples" "--benches"
    "--message-format"
  ];
  argsValid =
    builtins.isList cargoArgs
    && builtins.all (arg:
      builtins.isString arg && arg != ""
      && !(builtins.elem arg reservedArgs)
      && !(lib.hasPrefix "--package=" arg)
      && !(lib.hasPrefix "--message-format=" arg)
    ) cargoArgs;
  mkCompiled = name: args:
    import ./mk-cargo-test-package.nix {
      inherit pkgs src lockFile name;
      cargoArgs = args;
    };
  isolated =
    builtins.listToAttrs (map (name: {
      name = "test.rust-${name}";
      value = mkCompiled "rust-test-${name}" (
        [ "--package" name "--all-targets" ] ++ cargoArgs
      );
    }) selected);
  shared =
    let
      # The existing workspace adapter already owns per-crate wrappers.
      # Its explicit package list avoids unnecessary workspace parsing.
      existing = import ./mk-cargo-test-packages.nix {
        inherit pkgs src lockFile;
        packages = selected;
        compiledName = "rust-test-shared";
        cargoArgs =
          lib.concatMap (name: [ "--package" name ]) selected
          ++ [ "--all-targets" ] ++ cargoArgs;
      };
    in
    builtins.listToAttrs (map (name: {
      name = "test.rust-${name}";
      value = existing."test.cargo-${name}";
    }) selected);
  # The preexisting workspace helper owns member discovery and shared corpus
  # creation. Merely project its existing packages into the unified naming.
  allWorkspace =
    let
      existing = import ./mk-cargo-test-packages.nix {
        inherit pkgs src lockFile;
        cargoArgs = [ "--workspace" "--all-targets" ] ++ cargoArgs;
      };
    in
    builtins.listToAttrs (map (key: {
      name = "test.rust-${lib.removePrefix "test.cargo-" key}";
      value = existing.${key};
    }) (builtins.attrNames existing));
in
if crate != null && crates != null then
  fail "provide either crate or crates, not both"
else if !selectedValid then
  fail "crates must contain unique, valid nonempty Cargo package names"
else if !argsValid then
  fail "cargoArgs may contain extra Cargo flags but not target/package selectors"
else if !(builtins.elem selectedStrategy [ "shared" "isolated" ]) then
  fail "strategy must be shared or isolated"
else if selected == null && selectedStrategy != "shared" then
  fail "workspace-wide generation requires strategy = shared; provide crates for isolation"
else
  { packages =
      if selected == null then allWorkspace
      else if selectedStrategy == "shared" then shared
      else isolated;
  }
