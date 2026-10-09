{
  jobs,
  outputName ? "phenix-maintenance",
  workflowName ? "CI",
  mainBranch ? "main",
  gateName ? "Maintenance checks",
  checkoutAction ? "actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd",
  installNixAction ? "cachix/install-nix-action@a49548c11d9846ad46ecc0115273879b045f001c",
  cacheAction ? "actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830",
  cacheRestoreAction ? "actions/cache/restore@0057852bfaa89a56745cba8c7296529d2fc39830",
  cacheSaveAction ? "actions/cache/save@0057852bfaa89a56745cba8c7296529d2fc39830",
  nixCacheAction ? "nix-community/cache-nix-action@7df957e333c1e5da7721f60227dbba6d06080569",
  nixCache ? null,
  pullRequestJobs ? null,
  prImpact ? null,
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
    filter
    listToAttrs
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
  impactEnabled = prImpact != null && (prImpact.enable or false);
  impactedJobs = filter (job: (job.impact or null) != null && (pullRequestJobs == null || elem job.id pullRequestJobs)) jobs;
  impactConfig = {
    workspace = prImpact.workspace or "rust";
    jobs = listToAttrs (map (job: { name = job.id; value = job.impact; }) impactedJobs);
  } // (if (prImpact.verifiedShardChange or null) == null then { } else {
    # Optional content-verified change classification. These are source
    # locations, not dependency edges or an alternate package registry.
    inherit (prImpact) verifiedShardChange;
  });
  impactValid =
    if prImpact == null then true
    else if !isAttrs prImpact || !(isBool (prImpact.enable or false)) then
      fail "GitHub prImpact must be an attribute set with boolean enable"
    else if !impactEnabled then true
    else if !isString (prImpact.workspace or null) || (prImpact.workspace or "") == "" then
      fail "GitHub prImpact.workspace must be a non-empty Cargo workspace path"
    else if prImpact ? verifiedShardChange &&
      !(isAttrs prImpact.verifiedShardChange
        && builtins.all (name: builtins.hasAttr name prImpact.verifiedShardChange && isString (builtins.getAttr name prImpact.verifiedShardChange) && (builtins.getAttr name prImpact.verifiedShardChange) != "") [ "source" "list" "job" "workflow" ]) then
      fail "GitHub prImpact.verifiedShardChange must provide source, list, job and workflow paths"
    else if !(builtins.all (job:
      let rule = job.impact; in
      isAttrs rule && (rule.kind or null) == "cargo"
      && ((rule.packages or null) == null || (isList rule.packages && builtins.all isString rule.packages))
    ) impactedJobs) then
      fail "GitHub impacted jobs must declare Cargo package targets"
    else true;
  pullRequestJobsValid =
    if pullRequestJobs == null then
      true
    else if !isList pullRequestJobs || !(builtins.all isString pullRequestJobs) then
      fail "GitHub pullRequestJobs must be a list of CI stage identifiers"
    else if !(builtins.all (id: elem id jobIds) pullRequestJobs) then
      fail "GitHub pullRequestJobs contains an unknown CI stage"
    else if !(builtins.all (job: !(elem job.id pullRequestJobs) || builtins.all (need: elem need pullRequestJobs) job.needs) jobs) then
      fail "GitHub pullRequestJobs must include every dependency of the selected jobs"
    else
      true;

  nixCacheValid =
    if nixCache == null then
      true
    else if !isAttrs nixCache then
      fail "GitHub nixCache must be an attribute set"
    else if builtins.hasAttr "enable" nixCache && !isBool nixCache.enable then
      fail "GitHub nixCache.enable must be a boolean"
    else if builtins.hasAttr "saveOnDefaultBranch" nixCache && !isBool nixCache.saveOnDefaultBranch then
      fail "GitHub nixCache.saveOnDefaultBranch must be a boolean"
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

  renderCacheRestoreLines =
    cache:
    if cache == null then
      [ ]
    else if cache ? save then
      [
        "      - name: Restore shared cache"
        "        id: phenix-cache-restore"
        "        uses: ${cacheRestoreAction} # v4"
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
      ++ [ "" ]
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

  renderCacheSaveLines =
    cache:
    if cache == null || !(cache ? save) || !cache.save then
      [ ]
    else
      [
        "      - name: Save shared cache"
        ("        if: \${{ success() && steps.phenix-cache-restore.outputs.cache-hit != 'true'" + (if cache.saveOnDefaultBranch or false then " && github.ref == 'refs/heads/${mainBranch}'" else "") + " }}")
        "        uses: ${cacheSaveAction} # v4"
        "        with:"
        "          path: |"
      ]
      ++ map (path: "            ${path}") cache.paths
      ++ [
        "          key: ${yaml cache.key}"
        ""
      ];

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
      ++ (
        if nixCache.saveOnDefaultBranch or false then
          [ "          save: \${{ github.ref == 'refs/heads/${mainBranch}' }}" ]
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

  renderImpactJobLines =
    if !impactEnabled then
      [ ]
    else
      [
        "  impact:"
        "    if: github.event_name == 'pull_request' && github.event.pull_request.draft == false"
        "    name: Dependency impact plan"
        "    runs-on: ubuntu-latest"
        "    outputs:"
        "      jobs: \${{ steps.select.outputs.jobs }}"
        "      packages: \${{ steps.select.outputs.packages }}"
        "    steps:"
        "      - uses: ${checkoutAction} # v5"
        "        with:"
        "          fetch-depth: 0"
        ""
        "      - name: Plan affected checks"
        "        id: select"
        "        env:"
        "          GITHUB_BASE_REF: \${{ github.base_ref }}"
        "          PHENIX_IMPACT_CONFIG: ${yaml (toJSON impactConfig)}"
        "        run: |"
        "          set -euo pipefail"
        "          python3 - <<'PY'"
      ]
      ++ (map (line: if line == "" then "" else "          ${line}") (builtins.filter builtins.isString (builtins.split "\n" (builtins.readFile ./cargo-impact.py))))
      ++ [
        "          PY"
        ""
      ];

  renderJobLines =
    job:
    let
      affected = impactEnabled && (job.impact or null) != null && (pullRequestJobs == null || elem job.id pullRequestJobs);
      upstreamReady = concatStringsSep " && " (map (id: "needs.${id}.result == 'success'") job.needs);
      baseReady = if job.needs == [ ] then "true" else upstreamReady;
    in
    [
      "  ${job.id}:"
      (if affected then
        "    if: always() && (github.event_name != 'pull_request' && ${baseReady} || (github.event_name == 'pull_request' && github.event.pull_request.draft == false && needs.impact.result == 'success' && ${baseReady} && fromJSON(needs.impact.outputs.jobs || '{}')['${job.id}'] == true))"
      else if pullRequestJobs == null || elem job.id pullRequestJobs then
        "    if: github.event_name != 'pull_request' || github.event.pull_request.draft == false"
      else
        "    if: github.event_name != 'pull_request'")
      "    name: ${yaml job.name}"
      "    runs-on: ${yaml job.runner}"
      "    timeout-minutes: ${toString job.timeout}"
    ]
    ++ (if affected then
      [
        "    env:"
        "      PHENIX_IMPACT_PACKAGES: \${{ needs.impact.outputs.packages }}"
      ]
    else
      [ ])
    ++ renderNeedsLines (job.needs ++ (if affected then [ "impact" ] else [ ]))
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
    ++ renderCacheRestoreLines (job.cache or null)
    ++ concatLists (map (renderStepLines job) job.commands)
    ++ (if clean then renderCleanLines else [ ])
    ++ renderCacheSaveLines (job.cache or null)
    ++ [ "" ];

  gateNeedLines = (if impactEnabled then [ "      - impact" ] else [ ]) ++ map (id: "      - ${id}") jobIds;
  gateTestLines =
    (if impactEnabled then [ "          [[ '\${{ github.event_name }}' != pull_request || '\${{ needs.impact.result }}' == success ]]" ] else [ ])
    ++ map (
      id:
      let affected = impactEnabled && builtins.any (job: job.id == id) impactedJobs; in
      if pullRequestJobs != null && !(elem id pullRequestJobs) then
        "          [[ '\${{ needs.${id}.result }}' == success || ( '\${{ github.event_name }}' == pull_request && '\${{ needs.${id}.result }}' == skipped ) ]]"
      else if affected then
        "          [[ '\${{ needs.${id}.result }}' == success || ( '\${{ github.event_name }}' == pull_request && '\${{ needs.impact.result }}' == success && '\${{ fromJSON(needs.impact.outputs.jobs || '{}')['${id}'] }}' == false && '\${{ needs.${id}.result }}' == skipped ) ]]"
      else
        "          [[ '\${{ needs.${id}.result }}' == success ]]"
    ) jobIds;

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
  ++ renderImpactJobLines
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
assert pullRequestJobsValid;
assert impactValid;
if !validOutputName then
  fail "GitHub outputName must be a simple flake output identifier"
else if jobs == [ ] then
  fail "cannot render a GitHub workflow without CI jobs"
else
  joinLines workflowLines + "\n"
