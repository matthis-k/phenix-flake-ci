# Opinionated, composable Rust workspace CI entrypoint. No second test registry
# or scheduler: all outputs are normal Nix packages/checks, and the generic
# flake planner continues to discover their true derivation dependency graph.
{
  pkgs,
  src,
  lockFile,
  testGroups ? null,
  noDefaultFeatures ? false,
  cargoFmt ? true,
  clippy ? true,
  nixfmtFiles ? [ ],
  smoke ? { },
}:
let
  inherit (pkgs) lib;
  fail = message: throw "phenix-flake-ci: mkCargoWorkspace: ${message}";
  validName = text: builtins.isString text
    && builtins.match "[A-Za-z0-9][A-Za-z0-9_-]*" text != null;
  keysWithin = attrs: keys:
    builtins.all (key: builtins.elem key keys) (builtins.attrNames attrs);
  tests = import ./mk-cargo-test-checks.nix {
    inherit pkgs src lockFile noDefaultFeatures;
    groups = testGroups;
  };
  cargoLock = { lockFile = lockFile; };
  marker = ''
    mkdir -p "$out"
    touch "$out/passed"
  '';
  clippyCheck = pkgs.rustPlatform.buildRustPackage {
    pname = "cargo-clippy-check";
    version = "0.1.0";
    inherit src cargoLock;
    nativeBuildInputs = [ pkgs.clippy ];
    buildPhase = ''
      runHook preBuild
      cargo clippy --locked --offline --workspace --all-targets -- -D warnings
      runHook postBuild
    '';
    installPhase = marker;
    doCheck = false;
  };
  formatCheck = pkgs.runCommand "cargo-format-check" {
    nativeBuildInputs = [ pkgs.cargo pkgs.rustfmt ] ++ lib.optionals (nixfmtFiles != [ ]) [ pkgs.nixfmt ];
  } ''
    cp -r ${src} workspace
    chmod -R u+w workspace
    cd workspace
    cargo fmt --all --check
    ${lib.optionalString (nixfmtFiles != [ ]) "nixfmt --check ${lib.escapeShellArgs (map toString nixfmtFiles)}"}
    mkdir -p "$out"
  '';
  checkSmoke = name: spec:
    if !validName name || !builtins.isAttrs spec
       || !keysWithin spec [ "package" "example" "release" ]
       || !validName (spec.package or null)
       || !validName (spec.example or null)
       || !builtins.isBool (spec.release or true) then
      fail "smoke ${name} must name a Cargo package and example with an optional release boolean"
    else
      let
        release = spec.release or true;
        mode = if release then "release" else "debug";
      in
      pkgs.rustPlatform.buildRustPackage {
        pname = "cargo-${name}";
        version = "0.1.0";
        inherit src cargoLock;
        buildType = mode;
        buildPhase = ''
          runHook preBuild
          cargo build --locked --offline ${lib.optionalString release "--release"} \
            --package ${lib.escapeShellArg spec.package} \
            --example ${lib.escapeShellArg spec.example}
          runHook postBuild
        '';
        installPhase = ''
          runHook preInstall
          mkdir -p "$out/bin"
          install -m755 "target/${mode}/examples/${spec.example}" "$out/bin/${name}"
          "$out/bin/${name}"
          runHook postInstall
        '';
        doCheck = false;
        meta.mainProgram = name;
      };
  smokePackages = builtins.mapAttrs checkSmoke smoke;
  otherChecks = lib.optionalAttrs clippy { clippy = clippyCheck; }
    // lib.optionalAttrs cargoFmt { format = formatCheck; }
    // smokePackages;
  collision = builtins.filter (name: builtins.hasAttr name tests.checks)
    (builtins.attrNames otherChecks);
in
if !builtins.isBool noDefaultFeatures || !builtins.isBool cargoFmt || !builtins.isBool clippy then
  fail "noDefaultFeatures, cargoFmt and clippy must be booleans"
else if !builtins.isList nixfmtFiles || !builtins.all builtins.isPath nixfmtFiles then
  fail "nixfmtFiles must be a list of Nix paths"
else if !builtins.isAttrs smoke then
  fail "smoke must be an attribute set"
else if collision != [ ] then
  fail "check names collide with Cargo test groups: ${builtins.concatStringsSep ", " collision}"
else
  builtins.deepSeq smokePackages {
    checks = tests.checks // otherChecks;
    packages = tests.packages // smokePackages;
    inherit (tests) prebuildTargets;
  }
