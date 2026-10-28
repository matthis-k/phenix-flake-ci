# Pure Nix constructors for the same small test predicate IR that mkTest
# already consumes. They never execute, schedule, or add hidden assertions.
let
  predicate = operation: expected: { ${operation} = expected; };
  fileAssertion = path: assertion: { ${path} = assertion; };
in
rec {
  eq = predicate "eq";
  ne = predicate "ne";
  contains = predicate "contains";
  matches = predicate "matches";
  gt = predicate "gt";
  gte = predicate "gte";
  lt = predicate "lt";
  lte = predicate "lte";

  ref = step: field: { ref = { inherit step field; }; };
  jsonRef = step: field: pointer: {
    ref = { inherit step field; json = pointer; };
  };
  stdout = step: ref step "stdout";
  stderr = step: ref step "stderr";
  exitCode = step: ref step "exitCode";
  stdoutJson = step: pointer: jsonRef step "stdout" pointer;

  files = {
    exists = path: expected: fileAssertion path { exists = expected; };
    text = path: expectations: fileAssertion path { text = expectations; };
    json = path: assertions: fileAssertion path { json = assertions; };
    size = path: expected: fileAssertion path { size = expected; };
  };
}
