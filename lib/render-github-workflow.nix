{
  jobs,
  outputName ? "phenix-maintenance",
  workflowName ? "CI",
  mainBranch ? "main",
  gateName ? "Maintenance checks",
  checkoutAction ? "actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd",
  installNixAction ? "cachix/install-nix-action@a49548c11d9846ad46ecc0115273879b045f001c",
  cacheAction ? "actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830",
  nixCacheAction ? "nix-community/cache-nix-action@7df957e333c1e5da7721f60227dbba6d06080569",
  nixCache ? null,
  clean ? true,
}:
let
  inherit (builtins)
    attrNames
    concatLists
    concatStringsSep
    elem
    isAttrs
    isBool
    isList
    isString
    map
    match
    replaceStrings
    toJSON
    ;

  fail = message: throw "phenix-flake-ci: ${message}";
  scopeOutputName = import ./scope-output-name.nix;

  validOutputName = match "^[A-Za-z0-9][A-Za-z0-9_-]*$" outputName != null;
  yaml = toJSON;
  joinLines = concatStringsSep "\n";
  shellQuote = value: "'${replaceStrings [ "'" ] [ "'\"'\"'" ] value}'";

  jobIds = map (job: job.id) jobs;
  nixCacheEnabled = nixCache != null && (nixCache.enable or false);
  nixCacheJobs = if nixCache == null then [ ] else nixCache.jobs or [ ];
  nixCacheValid =
    if nixCache == null then
      true
    else if !isAttrs nixCache then
      fail "GitHub nixCache must be an attribute set"
    else if builtins.hasAttr "enable" nixCache && !isBool nixCache.enable then
      fail "GitHub nixCache.enable must be a boolean"
    else if !nixCacheEnabled then
      true
    else if nixCacheJobs == [ ] || !isList nixCacheJobs || !(builtins.all isString nixCacheJobs) then
      fail "GitHub nixCache.jobs must be a non-empty list of CI stage identifiers"
    else if !(builtins.all (job: elem job jobIds) nixCacheJobs) then
      fail "GitHub nixCache.jobs contains an unknown CI stage"
    else if !(builtins.hasAttr "primaryKey" nixCache) || !isString nixCache.primaryKey then
      fail "GitHub nixCache.primaryKey must be a string"
    else if
      builtins.hasAttr "restorePrefixesFirstMatch" nixCache
      && (!isList nixCache.restorePrefixesFirstMatch || !(builtins.all isString nixCache.restorePrefixesFirstMatch))
    then
      fail "GitHub nixCache.restorePrefixesFirstMatch must be a list of strings"
    else if
      builtins.hasAttr "restorePrefixesAllMatches" nixCache
      && (!isList nixCache.restorePrefixesAllMatches || !(builtins.all isString nixCache.restorePrefixesAllMatches))
    then
      fail "GitHub nixCache.restorePrefixesAllMatches must be a list of strings"
    else if builtins.hasAttr "gcMaxStoreSizeLinux" nixCache && !isString nixCache.gcMaxStoreSizeLinux then
      fail "GitHub nixCache.gcMaxStoreSizeLinux must be a string"
    else
      true;

  renderEnvLines =
    env:
    let
      names = attrNames env;
    in
    if names == [ ] then
      [ ]
    else
      [ "        env:" ] ++ map (name: "          ${name}: ${yaml env.${name}}") names;

  renderNeedsLines =
    needs: if needs == [ ] then [ ] else [ "    needs:" ] ++ map (need: "      - ${need}") needs;

  renderCacheLines =
    cache:
    if cache == null then
      [ ]
    else
      [
        "      - uses: ${cacheAction} # v4"
        "        with:"
        "          path: |"
      ]
      ++ map (path: "            ${path}") cache.paths
      ++ [ "          key: ${yaml cache.key}" ]
      ++ (
        if (cache.restoreKeys or [ ]) == [ ] then
          [ ]
        else
          [ "          restore-keys: |" ] ++ map (key: "            ${key}") cache.restoreKeys
      )
      ++ [ "" ];

  renderNixCacheLines =
    job:
    if !nixCacheEnabled || !(elem job.id nixCacheJobs) then
      [ ]
    else
      [
        "      - name: Restore and save Nix store"
        "        uses: ${nixCacheAction} # v7"
        "        with:"
        "          primary-key: ${yaml nixCache.primaryKey}"
      ]
      ++ (
        if (nixCache.restorePrefixesFirstMatch or [ ]) == [ ] then
          [ ]
        else
          [ "          restore-prefixes-first-match: |" ]
          ++ map (prefix: "            ${prefix}") nixCache.restorePrefixesFirstMatch
      )
      ++ (
        if (nixCache.restorePrefixesAllMatches or [ ]) == [ ] then
          [ ]
        else
          [ "          restore-prefixes-all-matches: |" ]
          ++ map (prefix: "            ${prefix}") nixCache.restorePrefixesAllMatches
      )
      ++ (
        if nixCache ? gcMaxStoreSizeLinux then
          [ "          gc-max-store-size-linux: ${yaml nixCache.gcMaxStoreSizeLinux}" ]
        else
          [ ]
      )
      ++ [ "" ];

  renderStepLines =
    job: command:
    let
      scopedOutput = scopeOutputName {
        inherit outputName;
        path = command.path;
      };
      invocation = toJSON {
        command = command.id;
        source = {
          type = "github-actions";
          job = job.id;
        };
      };
      runCommand = "printf '%s\\n' ${shellQuote invocation} | nix run --quiet .#${scopedOutput} -- invoke";
    in
    [ "      - name: ${yaml command.name}" ]
    ++ renderEnvLines job.env
    ++ [
      "        run: ${yaml runCommand}"
      ""
    ];

  renderCleanLines = [
    "      - name: Repository remains clean"
    "        if: \${{ !cancelled() }}"
    "        run: |"
    "          set -euo pipefail"
    "          git diff --exit-code"
    "          test -z \"$(git status --porcelain=v1 --untracked-files=all)\""
    ""
  ];

  renderJobLines =
    job:
    [
      "  ${job.id}:"
      "    if: github.event_name != 'pull_request' || github.event.pull_request.draft == false"
      "    name: ${yaml job.name}"
      "    runs-on: ${yaml job.runner}"
      "    timeout-minutes: ${toString job.timeout}"
    ]
    ++ renderNeedsLines job.needs
    ++ [
      "    steps:"
      "      - uses: ${checkoutAction} # v5"
      ""
      "      - uses: ${installNixAction} # v31"
      "        with:"
      "          github_access_token: \${{ secrets.GITHUB_TOKEN }}"
      "          extra_nix_config: |"
      "            experimental-features = nix-command flakes"
      "            accept-flake-config = true"
      "            max-jobs = auto"
      ""
    ]
    ++ renderNixCacheLines job
    ++ renderCacheLines (job.cache or null)
    ++ concatLists (map (renderStepLines job) job.commands)
    ++ (if clean then renderCleanLines else [ ])
    ++ [ "" ];

  gateNeedLines = map (id: "      - ${id}") jobIds;
  gateTestLines = map (id: "          [[ '\${{ needs.${id}.result }}' == success ]]") jobIds;

  workflowLines = [
    "# Generated by phenix-flake-ci. Edit the Nix maintenance declaration, not this file."
    "name: ${yaml workflowName}"
    ""
    "on:"
    "  pull_request:"
    "  push:"
    "    branches:"
    "      - ${yaml mainBranch}"
    "  workflow_dispatch:"
    ""
    "permissions:"
    "  contents: read"
    ""
    "concurrency:"
    "  group: ci-\${{ github.workflow }}-\${{ github.ref }}"
    "  cancel-in-progress: true"
    ""
    "jobs:"
  ]
  ++ concatLists (map renderJobLines jobs)
  ++ [
    "  checks:"
    "    if: always() && (github.event_name != 'pull_request' || github.event.pull_request.draft == false)"
    "    name: ${yaml gateName}"
    "    needs:"
  ]
  ++ gateNeedLines
  ++ [
    "    runs-on: ubuntu-latest"
    "    steps:"
    "      - name: Verify all maintenance jobs passed"
    "        run: |"
    "          set -euo pipefail"
  ]
  ++ gateTestLines;
in
assert nixCacheValid;
if !validOutputName then
  fail "GitHub outputName must be a simple flake output identifier"
else if jobs == [ ] then
  fail "cannot render a GitHub workflow without CI jobs"
else
  joinLines workflowLines + "\n"
