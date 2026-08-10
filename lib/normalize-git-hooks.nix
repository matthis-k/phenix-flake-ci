{
  name,
  commands,
  gitHooks ? { },
}:
let
  inherit (builtins)
    all
    attrNames
    elem
    filter
    hasAttr
    head
    isAttrs
    isBool
    isList
    isString
    match
    tail
    ;

  fail = message: throw "phenix-flake-ci: ${message}";

  validCommandName =
    commandName: isString commandName && match "^[A-Za-z0-9][A-Za-z0-9_-]*$" commandName != null;

  pathExists =
    path: nodes:
    if path == [ ] then
      false
    else
      let
        segment = head path;
        rest = tail path;
      in
      hasAttr segment nodes
      && (
        rest == [ ]
        || (
          let
            node = nodes.${segment};
          in
          isAttrs node && hasAttr "commands" node && isAttrs node.commands && pathExists rest node.commands
        )
      );

  enabled = gitHooks.enable or false;
  preCommit = gitHooks.preCommit or [ ];
  unknownKeys = filter (key: !(elem key [ "enable" "preCommit" ])) (attrNames gitHooks);
in
if !isAttrs gitHooks then
  fail "`${name}`: gitHooks must be an attribute set"
else if unknownKeys != [ ] then
  fail "`${name}`: unknown gitHooks options: ${builtins.concatStringsSep ", " unknownKeys}"
else if hasAttr "enable" gitHooks && !isBool gitHooks.enable then
  fail "`${name}`: gitHooks.enable must be a boolean"
else if !isList preCommit || !(all validCommandName preCommit) then
  fail "`${name}`: gitHooks.preCommit must be a command path list"
else if enabled && preCommit == [ ] then
  fail "`${name}`: enabled git hooks require gitHooks.preCommit"
else if enabled && !(pathExists preCommit commands) then
  fail "`${name}`: gitHooks.preCommit does not reference a declared command"
else
  {
    inherit enabled preCommit;
  }
