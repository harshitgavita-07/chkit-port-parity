# chkit-port-parity

One command line, two implementations: run identical CLI scenarios against
[chkit](https://github.com/obsessiondb/chkit) (TypeScript) and `chkit-py` (Python)
and flag where the behaviour a user sees diverges.

## Why

ObsessionDB's own blog describes chkit-py as "ported to Python at parity with the
TypeScript original", verified by "1,100+ tests (most ported case-for-case from the
TypeScript suites)". Ported tests check each implementation against expectations
written for that implementation. Nothing in the repo executes the *same* scenario
against *both* published CLIs and diffs what a user would actually see - and the
issue tracker shows the cost of that: a steady stream of "works in TS, not in
chkit-py" reports (#685-#692 this week alone), each found one at a time.

This repo is that missing net: 12 CLI-level scenarios over the surface every user
touches before a database exists (argument handling, `init` scaffolding, config
discovery, connection-failure shape), executed against both published CLIs in fresh
temp dirs, with exit codes and behavioural *classes* compared (not wording - two
implementations may phrase the same behaviour differently, and that's fine).

## Current results

Published artifacts, 2026-11-14: `chkit@0.2.0-beta.16` (npm, via documented `npx`
path, Node v22.23.4) vs `chkit-py 0.3.0` (PyPI), Ubuntu, no ClickHouse server.

```
parity    no-args-shows-help            ts(exit=0,ok) py(exit=0,ok)
DIVERGES  unknown-command-no-config     ts(exit=1,config_missing) py(exit=2,usage_error)
DIVERGES  unknown-flag-no-config        ts(exit=1,config_missing) py(exit=2,usage_error)
parity    init-empty-dir                ts(exit=0,ok) py(exit=0,ok)
parity    status-no-config              ts(exit=1,config_missing) py(exit=1,config_missing)
parity    drift-no-config               ts(exit=1,config_missing) py(exit=1,config_missing)
parity    generate-no-config            ts(exit=1,config_missing) py(exit=1,config_missing)
DIVERGES  init-then-status              ts(exit=1,other_error) py(exit=1,connection_error)
parity    init-twice                    ts(exit=0,ok) py(exit=0,ok)
parity    check-no-config               ts(exit=1,config_missing) py(exit=1,config_missing)
parity    plugin-list                   ts(exit=1,config_missing) py(exit=1,config_missing)
DIVERGES  unknown-subcommand-flag-no-config ts(exit=1,config_missing) py(exit=2,usage_error)

12 scenarios, 4 diverging
```

### What the divergences are

1. **Usage errors are masked by config checks (3 scenarios, one root).**
   `chkit frobnicate` or `chkit --bogus-flag` with no config present: the TS CLI
   answers "Config not found" (exit 1); the Python CLI answers with a usage error
   (exit 2). A user with a typo is told to fix a config that isn't the problem.
   This one is *already on their radar* - it is exactly issue #687, with fix PR #691
   open. The harness catches the whole class, including the subcommand-flag case.

2. **`init` -> `status` dead-ends with a misleading message (not currently filed).**
   Follow the README's own next-steps after a successful `npx chkit init`:
   `npx chkit status` answers `clickhouse config is required for status (journal is
   stored in ClickHouse)` - but the config file exists; `init` just wrote it. The
   real state is "no ClickHouse connection configured/reachable". The Python CLI in
   the identical state says exactly that: `Could not connect to ClickHouse at
   http://localhost:8123 (connection refused)` plus a `CLICKHOUSE_URL` hint. Root
   cause in TS: `status.ts` throws when `pluginContext.hasExecutor` is false, and
   the message blames the config. They aligned a no-target message once before
   (#459, closed) - the published builds disagree again.

### Deliberately not flagged

- File-set comparison normalises `.ts`/`.py` and ignores `package.json` /
  lockfiles - TS-ecosystem artifacts with no Python counterpart.
- `init`'s dependency auto-install is documented as won't-port in
  `chkit_python/DRIFT.md` ("--auto-deps: not portable by Python convention"), so
  the harness does not test it. (Side observation, not a harness result: the TS
  side defaults to bun when `npm_config_user_agent` is unset - `deps.ts`
  `detectPackageManager` - so invoking the bin directly instead of the documented
  `npx` path tries a bun install even when bun isn't installed.)

## Honest limits

- **No-server surface only.** Commands that need a live ClickHouse (migrate,
  drift with a target, check gates) are out of scope here; a dockerised
  ClickHouse lane would extend the same scenario files.
- **Classes, not wording.** The harness proves behavioural class and exit-code
  divergence; identical classes can still hide wording differences a user would
  notice.
- **One platform, two pinned versions.** Results above are exact for the listed
  versions on Linux; the harness takes any two commands, so re-running against
  newer releases is one flag change.
- Divergences are cross-referenced against the repo's open issues/PRs and
  DRIFT.md by hand; that mapping is stated per finding above, not automated.

## Run it

```
npm i chkit@0.2.0-beta.16          # or any version
uv pip install chkit-py==0.3.0     # or any version
python3 harness.py --scenarios scenarios --ts "npx chkit" --py ".venv/bin/chkit"
```

Exit code is 1 when any scenario diverges, so it drops straight into CI.

## Tests

```
python3 -m unittest test_harness -v
```

12 tests: classifier rules (including "help text must not classify as a usage
error"), comparison rules (extension normalisation, ignore lists, per-port
assertions), and two end-to-end runs against fake ports - one pair identical
(passes), one pair planted with an exit/usage divergence (detected).

## Layout

- `harness.py` - the runner/classifier/comparator, stdlib only
- `scenarios/*.json` - one scenario per file: argv, optional setup files,
  optional pre-steps (e.g. `init` before `status`), assertions
- `test_harness.py` - unit + fake-port end-to-end tests
