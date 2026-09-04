{
  name,
  description,
  commands,
  ciSchemaVersion,
  jobs,
  matrix,
  gitHooks,
}:
let
  inherit (builtins)
    attrNames
    concatLists
    concatStringsSep
    hasAttr
    listToAttrs
    map
    ;

  pathId = path: concatStringsSep "/" path;

  flattenNode =
    path: node:
    let
      children = node.commands or { };
      childNames = node.order or (attrNames children);
    in
    [
      {
        inherit path node childNames;
      }
    ]
    ++ concatLists (
      map (childName: flattenNode (path ++ [ childName ]) children.${childName}) childNames
    );

  flattened = concatLists (
    map (commandName: flattenNode [ commandName ] commands.${commandName}) (attrNames commands)
  );

  commandEntries = map (
    entry:
    let
      id = pathId entry.path;
    in
    {
      name = id;
      value = {
        inherit id;
        inherit (entry) path;
        description = entry.node.description or "";
        kind = if hasAttr "exec" entry.node then "exec" else "group";
        children = map (childName: pathId (entry.path ++ [ childName ])) entry.childNames;
        dependencies = map pathId (entry.node.dependencies or [ ]);
      };
    }
  ) flattened;

  hookIndex =
    if gitHooks.enabled then
      {
        "pre-commit" = {
          command = pathId gitHooks.preCommit;
        };
      }
    else
      { };
in
{
  schemaVersion = 1;
  cli = {
    inherit name description;
  };
  commandOrder = map (entry: pathId entry.path) flattened;
  commands = listToAttrs commandEntries;
  ci = {
    schemaVersion = ciSchemaVersion;
    inherit jobs matrix;
  };
  hooks = hookIndex;
}
