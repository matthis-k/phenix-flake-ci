# Discover Cargo workspace members and expose one runnable test.* package
# per crate. All wrappers reference the SAME compiled test corpus, so Nix
# schedules compilation once and test jobs run only already-built binaries.
{
  pkgs,
  src,
  lockFile,
  compiledName ? "cargo-workspace-tests",
  cargoArgs ? [ "--workspace" "--all-targets" ],
}:
let
  inherit (pkgs) lib;
  manifest = builtins.fromTOML (builtins.readFile "${src}/Cargo.toml");
  workspace = manifest.workspace or { };
  declared = workspace.members or [ ];
  exclude = workspace.exclude or [ ];
  allowedMember = member:
    if !builtins.isString member || member == ""
       || lib.hasPrefix "/" member || lib.hasInfix ".." member then
      throw "phenix-flake-ci: invalid Cargo workspace member path"
    else
      member;
  expand = member:
    let
      checked = allowedMember member;
    in
    if lib.hasSuffix "/*" checked then
      let
        prefix = lib.removeSuffix "/*" checked;
        dirs = builtins.readDir "${src}/${prefix}";
      in
      map (child: "${prefix}/${child}") (
        builtins.filter (child:
          dirs.${child} == "directory"
          && builtins.pathExists "${src}/${prefix}/${child}/Cargo.toml"
        ) (builtins.attrNames dirs))
    else if lib.hasInfix "*" checked || lib.hasInfix "?" checked then
      throw "phenix-flake-ci: only exact members and trailing /* globs are supported"
    else
      [ checked ];
  memberPaths = lib.unique (
    (lib.optionals (manifest ? package) [ "." ])
    ++ lib.concatMap expand declared
  );
  includedPaths = builtins.filter (path:
    !(builtins.elem path exclude)
    && builtins.pathExists "${src}/${path}/Cargo.toml"
  ) memberPaths;
  names = map (path:
    let
      file = builtins.fromTOML (builtins.readFile "${src}/${path}/Cargo.toml");
      name = file.package.name or null;
    in
    if !builtins.isString name
       || builtins.match "[A-Za-z0-9][A-Za-z0-9_-]*" name == null then
      throw "phenix-flake-ci: invalid or missing Cargo workspace package name"
    else
      name
  ) includedPaths;
  uniqueNames = lib.unique names;
  compiled = import ./mk-cargo-test-package.nix {
    inherit pkgs src lockFile cargoArgs;
    name = compiledName;
  };
in
if uniqueNames == [ ] || builtins.length uniqueNames != builtins.length names then
  throw "phenix-flake-ci: Cargo workspace has no valid unique test packages"
else
  builtins.listToAttrs (map (package: {
    name = "test.cargo-${package}";
    value = pkgs.writeShellApplication {
      name = "cargo-test-${package}";
      text = ''
        exec ${compiled}/bin/${compiledName} --package ${lib.escapeShellArg package} "$@"
      '';
    };
  }) uniqueNames)
