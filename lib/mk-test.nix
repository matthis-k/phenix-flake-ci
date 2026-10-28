{
  pkgs,
  name,
  command ? null,
  steps ? null,
  expect ? { },
  env ? { },
  fixtures ? { },
}:
let
  fail = reason: throw "phenix-flake-ci.mkTest: ${reason}";
  valid =
    if !builtins.isString name || builtins.match "[A-Za-z][A-Za-z0-9_-]*" name == null then
      fail "name must start with a letter and contain only letters, numbers, underscores or hyphens"
    else if (command == null) == (steps == null) then
      fail "provide exactly one of command or steps"
    else if command != null && !builtins.isAttrs command then
      fail "command must be an attribute set"
    else if steps != null && (!builtins.isList steps || steps == [ ] || !(builtins.all builtins.isAttrs steps)) then
      fail "steps must be a nonempty list of attribute sets"
    else if !builtins.isAttrs expect || !builtins.isAttrs env || !builtins.isAttrs fixtures then
      fail "expect, env, and fixtures must be attribute sets"
    else if !(builtins.all builtins.isString (builtins.attrValues env)) then
      fail "env values must be strings"
    else if !(builtins.all builtins.isString (builtins.attrValues fixtures)) then
      fail "fixture values must be strings"
    else true;
  spec = { inherit name expect env fixtures; }
    // (if command != null then { inherit command; } else { inherit steps; });
in
assert valid;
pkgs.runCommand "test-${name}" {
  testSpec = builtins.toJSON spec;
  passAsFile = [ "testSpec" ];
} ''
  ${pkgs.python3}/bin/python ${./test-runner.py} --spec "$testSpecPath" --output "$out"
''
