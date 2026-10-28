# Opt-in Cargo adapter: turns a workspace's *compiled* Rust test binaries
# into one runnable Nix package. The generic CI engine only sees test.*.
{
  pkgs,
  src,
  lockFile,
  name ? "cargo-workspace-tests",
  version ? "0.1.0",
  cargoArgs ? [ "--workspace" "--all-targets" ],
}:
let
  inherit (pkgs) lib;
  valid =
    builtins.isString name
    && builtins.match "[A-Za-z0-9][A-Za-z0-9_-]*" name != null
    && builtins.isList cargoArgs
    && builtins.all builtins.isString cargoArgs
    && cargoArgs != [ ];
in
assert valid;
pkgs.rustPlatform.buildRustPackage {
  pname = name;
  inherit version src;
  cargoLock.lockFile = lockFile;
  doCheck = false;
  nativeBuildInputs = [ pkgs.python3 ];

  # Build within the immutable source tree, using the target directory in the
  # sandbox. That preserves stable CARGO_MANIFEST_DIR paths for tests which
  # read fixtures via env!("CARGO_MANIFEST_DIR") after installation.
  buildPhase = ''
    runHook preBuild
    export CARGO_TARGET_DIR="$TMPDIR/cargo-test-target"
    export CARGO_HOME="''${CARGO_HOME:-$TMPDIR/cargo-home}"
    mkdir -p "$CARGO_TARGET_DIR" "$CARGO_HOME"
    if [ -f .cargo/config.toml ]; then
      cp .cargo/config.toml "$CARGO_HOME/config.toml"
    fi
    cd ${src}
    cargo test --locked --offline --no-run --message-format=json ${lib.escapeShellArgs cargoArgs} > "$TMPDIR/cargo-test-artifacts.jsonl"
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p "$out/bin" "$out/libexec"
    ${pkgs.python3}/bin/python ${./cargo-test-artifacts.py} \
      --input "$TMPDIR/cargo-test-artifacts.jsonl" --output "$out/libexec"
    cat > "$out/bin/${name}" <<'SCRIPT'
    #!${pkgs.runtimeShell}
    set -euo pipefail
    cd ${src}
    found=0
    for suite in "@out@"/libexec/*; do
      test -x "$suite"
      found=1
      "$suite" "$@"
    done
    test "$found" = 1
    SCRIPT
    substituteInPlace "$out/bin/${name}" --replace-fail '@out@' "$out"
    chmod +x "$out/bin/${name}"
    runHook postInstall
  '';

  meta.mainProgram = name;
}
