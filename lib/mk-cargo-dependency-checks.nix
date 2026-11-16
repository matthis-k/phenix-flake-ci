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
       || builtins.any (field: !(builtins.elem field [ "packages" "profile" "args" "src" "targets" ]))
         (builtins.attrNames spec)
       || !validNames (spec.packages or null)
       || !validName (spec.profile or "default")
       || !(builtins.hasAttr (spec.profile or "default") profiles)
       || !validArgv (spec.args or [ ])
       || (spec ? targets && !validArgv spec.targets)
    then fail "invalid packages, profile, or extra arguments in test group ${name}"
    else spec // { profile = spec.profile or "default"; args = spec.args or [ ]; }
  ) groups;
  # Default target selectors and feature flags share a backwards-compatible
  # profile list. A group's target override changes *only* target selection;
  # feature/compilation flags still derive from the dependency profile.
  targetSelectors = [
    "--all-targets" "--lib" "--tests" "--doc"
    "--bins" "--examples" "--benches"
  ];
  dependencyArgs = profile:
    builtins.filter (flag: !(builtins.elem flag targetSelectors)) profiles.${profile};
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
        ++ dependencyArgs profile
      );
    });
  }) usedProfiles);
  # Optional per-group sources are overlays of real crate files, not
  # alternative Cargo workspaces. Keep every member manifest in Crane's
  # dummy source, preserving the original workspace/lockfile resolution.
  # Group builds depend only on their selected real sources plus common
  # dependency metadata, not on unrelated crate implementation changes.
  dummyWorkspace = craneLib.mkDummySrc { inherit src; };
  checks = builtins.mapAttrs (name: spec:
    craneLib.cargoTest (common // {
      pname = "ci-test-${name}";
      src = if spec ? src then dummyWorkspace else src;
      postUnpack =
        if spec ? src then ''
          # Overlay group implementation files without deleting manifests
          # or dummy targets of unselected workspace members.
          chmod -R u+w "$sourceRoot"
          # Caller overlays may contain a rewritten root manifest or lock.
          # Never let those change the complete Cargo resolution context.
          cp "$sourceRoot/Cargo.toml" "$TMPDIR/ci-full-Cargo.toml"
          cp "$sourceRoot/Cargo.lock" "$TMPDIR/ci-full-Cargo.lock"
          cp -r --no-preserve=mode ${spec.src}/. "$sourceRoot"/
          cp "$TMPDIR/ci-full-Cargo.toml" "$sourceRoot/Cargo.toml"
          cp "$TMPDIR/ci-full-Cargo.lock" "$sourceRoot/Cargo.lock"
        '' else "";
      cargoArtifacts = deps.${spec.profile};
      # These are terminal checks: retaining an entire (potentially multi-GB)
      # Cargo target corpus after each independent test suite is redundant.
      # Only buildDepsOnly publishes reusable compiler artifacts.
      doInstallCargoArtifacts = false;
      cargoExtraArgs = lib.escapeShellArgs (
        [ "--locked" ]
        ++ lib.concatMap (package: [ "--package" package ]) spec.packages
        ++ (if spec ? targets then dependencyArgs spec.profile ++ spec.targets else profiles.${spec.profile})
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
  # Force the small validated declaration, not arbitrary Nix derivations
  # embedded in group.src. Deep evaluation of a derivation attrset can walk
  # recursive nixpkgs internals and overflow even for valid generated sources.
  builtins.deepSeq
    (builtins.mapAttrs (_: spec: {
      inherit (spec) packages profile args;
    }) validatedGroups)
    { inherit checks packages; }
