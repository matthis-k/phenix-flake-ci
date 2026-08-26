{
  outputName,
  path,
}:
let
  id = builtins.concatStringsSep "/" path;
  digest = builtins.substring 0 12 (builtins.hashString "sha256" id);
in
"${outputName}-command-${digest}"
