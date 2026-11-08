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
    # Nix's cargoSetupPostUnpackHook wrote the offline vendoring config in
    # the writable unpack tree. Cargo runs in the original immutable source
    # to preserve CARGO_MANIFEST_DIR for fixtures, so pass that config
    # explicitly. Its @vendor@ placeholder was substituted by Nix already.
    # Nix calls postUnpack hooks before/around changing into sourceRoot.
    # The generated config can therefore be under NIX_BUILD_TOP rather than
    # the unpacked source. Use only the actual Nix-generated vendor config.
    vendor_config=""
    for candidate in "$NIX_BUILD_TOP/.cargo/config.toml"       "$NIX_BUILD_TOP/$sourceRoot/.cargo/config.toml"       "$PWD/.cargo/config.toml"; do
      if test -s "$candidate" && grep -q 'vendored-sources' "$candidate"; then
        vendor_config="$candidate"
        break
      fi
    done
    if [ -z "$vendor_config" ]; then
      echo "missing Nix-generated offline Cargo vendor configuration" >&2
      find "$NIX_BUILD_TOP" -maxdepth 4 -path '*/.cargo/config.toml' -print >&2
      exit 1
    fi
    cd ${src}
    cargo --config "$vendor_config" metadata --locked --offline --no-deps --format-version 1 > "$TMPDIR/cargo-metadata.json"
    cargo --config "$vendor_config" test --locked --offline --no-run --message-format=json ${lib.escapeShellArgs cargoArgs} > "$TMPDIR/cargo-test-artifacts.jsonl"
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p "$out/bin" "$out/libexec"
    ${pkgs.python3}/bin/python ${./cargo-test-artifacts.py} \
      --input "$TMPDIR/cargo-test-artifacts.jsonl" \
      --metadata "$TMPDIR/cargo-metadata.json" --output "$out/libexec"
    cat > "$out/bin/${name}" <<'SCRIPT'
    #!${pkgs.runtimeShell}
    set -euo pipefail
    # Use the same manifest-aware runner for direct packages and named Nix
    # checks. This preserves Cargo's per-crate current-directory semantics.
    exec ${pkgs.python3}/bin/python3 ${./cargo-test-runner.py} \
      --manifest "@out@/cargo-tests.json" \
      --bin-dir "@out@/libexec" \
      --cwd "${src}" "$@"
    SCRIPT
    substituteInPlace "$out/bin/${name}" --replace-fail '@out@' "$out"
    chmod +x "$out/bin/${name}"
    runHook postInstall
  '';

  meta.mainProgram = name;
}
