# One Cargo test compilation per compatible feature profile; many independent Nix
# checks execute its already-built binaries. The generic planner discovers the
# returned checks normally: no Cargo knowledge in the Nix scheduler.
{
  pkgs,
  src,
  lockFile,
  groups ? null,
  noDefaultFeatures ? false,
  profiles ? {
    default = [ "--workspace" "--all-targets" ];
    "no-default" = [ "--workspace" "--no-default-features" "--lib" "--tests" ];
  },
}:
let
  inherit (pkgs) lib;
  fail = msg: throw "phenix-flake-ci: mkCargoTestChecks: ${msg}";
  validName = value:
    builtins.isString value
    && builtins.match "[A-Za-z0-9][A-Za-z0-9_-]*" value != null;
  validNames = values:
    builtins.isList values && builtins.all validName values
    && builtins.length values == builtins.length (lib.unique values);
  keysWithin = attrs: allowed:
    builtins.all (key: builtins.elem key allowed) (builtins.attrNames attrs);
  sourceValid = builtins.pathExists "${src}/Cargo.toml"
    && builtins.pathExists lockFile;
  profilesValid = builtins.isAttrs profiles && profiles != { }
    && builtins.all (
      name: validName name && builtins.isList profiles.${name}
        && profiles.${name} != [ ]
        && builtins.all builtins.isString profiles.${name}
    ) (builtins.attrNames profiles);
  checkGroup = name: group:
    if !validName name then
      fail "check names must contain only letters, digits, underscore and hyphen"
    else if !builtins.isAttrs group || !keysWithin group [ "packages" "profile" "targets" ] then
      fail "check ${name} has unknown fields"
    else
      let
        packageNames = group.packages or [ ];
        profile = group.profile or "default";
        targets = group.targets or { };
        tests = targets.tests or [ ];
        includeLib = targets.lib or false;
        hasTargets = group ? targets;
      in
      if !validNames packageNames then
        fail "check ${name} has invalid or duplicate Cargo package names"
      else if !validName profile || !(builtins.hasAttr profile profiles) then
        fail "check ${name} references an undefined compilation profile"
      else if !builtins.isAttrs targets || !keysWithin targets [ "lib" "tests" ] then
        fail "check ${name} has invalid target selectors"
      else if !validNames tests || !builtins.isBool includeLib then
        fail "check ${name} has invalid test names or lib selection"
      else if hasTargets && !includeLib && tests == [ ] then
        fail "check ${name} declares an empty target selector"
      else
        { inherit profile tests includeLib; packages = packageNames; };
  discovered = import ./cargo-workspace-members.nix { inherit lib src; };
  selectedGroups =
    if groups == null then
      builtins.listToAttrs (map (package: {
        name = "cargo-${package}";
        value.packages = [ package ];
      }) discovered)
    else
      groups;
  effectiveGroups = selectedGroups // lib.optionalAttrs noDefaultFeatures {
    "cargo-no-default".profile = "no-default";
  };
  groupNames = builtins.attrNames effectiveGroups;
  checked = builtins.listToAttrs (map (name: {
    inherit name;
    value = checkGroup name effectiveGroups.${name};
  }) groupNames);
  usedProfiles = lib.unique (map (name: checked.${name}.profile) groupNames);
  compiled = builtins.listToAttrs (map (profile: {
    name = profile;
    value = import ./mk-cargo-test-package.nix {
      inherit pkgs src lockFile;
      name = "cargo-tests-${profile}";
      cargoArgs = profiles.${profile};
    };
  }) usedProfiles);
  checks = builtins.mapAttrs (
    name: group:
    let
      args =
        (lib.concatMap (package: [ "--package" package ]) group.packages)
        ++ lib.optionals group.includeLib [ "--lib" ]
        ++ lib.concatMap (test: [ "--test" test ]) group.tests;
      corpus = compiled.${group.profile};
    in
    pkgs.runCommand "cargo-check-${name}" { } ''
      ${pkgs.python3}/bin/python3 ${./cargo-test-runner.py} \
        --manifest ${corpus}/cargo-tests.json \
        --bin-dir ${corpus}/libexec \
        --cwd ${src} ${lib.escapeShellArgs args}
      mkdir -p "$out"
    ''
  ) checked;
  packages = builtins.listToAttrs (lib.concatMap (profile: [
    {
      name = "cargo-tests-${profile}";
      value = compiled.${profile};
    }
    {
      # Ordinary package alias consumed by generic planner autoPrebuild.
      # Both names refer to exactly one immutable compiled corpus derivation.
      name = "ci-prebuild-cargo-tests-${profile}";
      value = compiled.${profile};
    }
  ]) usedProfiles);
  prebuildTargets = map (
    profile: ".#packages.${pkgs.stdenv.hostPlatform.system}.cargo-tests-${profile}"
  ) usedProfiles;
in
if groups != null && !builtins.isAttrs groups then
  fail "groups must be an attribute set or null for automatic workspace discovery"
else if !builtins.isBool noDefaultFeatures then
  fail "noDefaultFeatures must be boolean"
else if noDefaultFeatures && groups != null && groups ? cargo-no-default then
  fail "cargo-no-default is reserved when noDefaultFeatures is enabled"
else if effectiveGroups == { } then
  fail "at least one Cargo check group is required"
else if !sourceValid || !profilesValid then
  fail "invalid Cargo workspace source, lock file or compilation profiles"
else
  builtins.deepSeq checked {
    inherit checks packages prebuildTargets;
  }
