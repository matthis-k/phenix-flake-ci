# Cargo compiles registry/workspace dependency scaffolding once per feature
# profile, then each named test group compiles its real crates independently.
# It uses crane's existing buildDepsOnly/cargoTest primitives; no second
# scheduler, test registry, target-directory copy mechanism or Cargo wrapper.
{
  pkgs,
  craneLib,
  src,
  lockFile,
  groups,
  profiles ? {
    default = [ "--all-targets" ];
    "no-default" = [ "--no-default-features" "--lib" "--tests" ];
  },
}:
let
  inherit (pkgs) lib;
  fail = message: throw "phenix-flake-ci: mkCargoDependencyChecks: ${message}";
  validName = name:
    builtins.isString name
    && builtins.match "[A-Za-z0-9][A-Za-z0-9_-]*" name != null;
  validNames = names:
    builtins.isList names && names != [ ]
    && builtins.all validName names
    && builtins.length names == builtins.length (lib.unique names);
  validArgv = argv:
    builtins.isList argv
    && builtins.all (item: builtins.isString item && item != "") argv;
  validatedGroups = builtins.mapAttrs (name: spec:
    if !validName name || !builtins.isAttrs spec
       || builtins.any (field: !(builtins.elem field [ "packages" "profile" "args" "src" "postUnpack" ]))
         (builtins.attrNames spec)
       || !validNames (spec.packages or null)
       || !validName (spec.profile or "default")
       || !(builtins.hasAttr (spec.profile or "default") profiles)
       || !validArgv (spec.args or [ ])
       || !(builtins.isString (spec.postUnpack or ""))
    then fail "invalid packages, profile, or extra arguments in test group ${name}"
    else spec // { profile = spec.profile or "default"; args = spec.args or [ ]; }
  ) groups;
  usedProfiles = lib.unique (map
    (name: validatedGroups.${name}.profile)
    (builtins.attrNames validatedGroups));
  common = {
    inherit src;
    cargoLock = lockFile;
    version = "0.1.0";
    # Crane defaults to release; match cargo test's normal dev profile.
    CARGO_PROFILE = "";
  };
  deps = builtins.listToAttrs (map (profile: {
    name = profile;
    value = craneLib.buildDepsOnly (common // {
      pname = "ci-deps-${profile}";
      # Dummy workspace sources: real code compile errors belong exclusively
      # to each downstream group, not to this shared compilation barrier.
      # Crane's buildDepsOnly already appends --all-targets to cargo check.
      # Target selectors (--lib/--tests/--all-targets) belong to the real
      # cargoTest leaf only; dependency preparation needs feature flags.
      cargoExtraArgs = lib.escapeShellArgs (
        [ "--locked" "--workspace" ]
        ++ builtins.filter (flag: !builtins.elem flag [
          "--all-targets" "--lib" "--tests"
        ]) profiles.${profile}
      );
    });
  }) usedProfiles);
  checks = builtins.mapAttrs (name: spec:
    craneLib.cargoTest (common // {
      pname = "ci-test-${name}";
      # A group can provide a narrowed workspace source and manifest rewrite.
      # This preserves Nix's incremental rebuild granularity: changing an
      # unrelated crate must not invalidate every independent test check.
      src = spec.src or src;
      postUnpack = spec.postUnpack or "";
      cargoArtifacts = deps.${spec.profile};
      # These are terminal checks: retaining an entire (potentially multi-GB)
      # Cargo target corpus after each independent test suite is redundant.
      # Only buildDepsOnly publishes reusable compiler artifacts.
      doInstallCargoArtifacts = false;
      cargoExtraArgs = lib.escapeShellArgs (
        [ "--locked" ]
        ++ lib.concatMap (package: [ "--package" package ]) spec.packages
        ++ profiles.${spec.profile}
        ++ spec.args
      );
      cargoTestExtraArgs = "--no-fail-fast";
    })
  ) validatedGroups;
  # The optional prebuild aliases are ordinary packages. The existing
  # staged Nix DAG discovers their derivations; no Cargo-aware scheduling.
  packages = builtins.listToAttrs (map (profile: {
    name = "ci-prebuild-cargo-deps-${profile}";
    value = deps.${profile};
  }) usedProfiles);
in
if !builtins.isAttrs groups || groups == { } then
  fail "groups must be a non-empty attribute set"
else if !builtins.isAttrs profiles || profiles == { } || !builtins.all
  (name: validName name && validArgv profiles.${name})
  (builtins.attrNames profiles) then
  fail "profiles must map names to lists of Cargo flags"
else if !builtins.pathExists "${src}/Cargo.toml" || !builtins.pathExists lockFile then
  fail "src must contain a Cargo workspace and lockFile must exist"
else
  builtins.deepSeq validatedGroups { inherit checks packages; }
