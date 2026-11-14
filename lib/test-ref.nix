# Runtime fixture references are tagged data, never shell interpolation.
{
  socket = name:
    if !builtins.isString name || builtins.match "[A-Za-z][A-Za-z0-9_-]*" name == null then
      throw "phenix-flake-ci.testRef.socket: invalid socket name"
    else
      { ref = { fixture = name; field = "path"; }; };
}
