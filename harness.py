#!/usr/bin/env python3
"""chkit-port-parity: run identical CLI scenarios against chkit (TypeScript)
and chkit-py (Python) and flag behavioural divergences.

ObsessionDB ships chkit as two implementations that promise parity ("ported
to Python at parity", with intended differences logged in chkit_python/DRIFT.md).
Their tests are ported case-for-case per implementation, but nothing executes
the SAME scenario against BOTH published CLIs and diffs what a user sees.
This harness does exactly that, on the no-server surface (argument handling,
init scaffolding, config discovery, connection failure shape) - the commands
that must behave before a database exists.

Stdlib only. Usage:
    python3 harness.py --scenarios scenarios --ts "npx chkit" --py ".venv/bin/chkit"
Exit code 1 when any scenario diverges, 0 when all hold.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Output classification.
#
# Raw stdout/stderr wording legitimately differs between two implementations
# (typer vs a hand-rolled TS CLI). What must match for a drop-in port is the
# behavioural CLASS: did it work, was it a usage error, did it complain about
# a missing config, did it fail to reach the server, or did it crash?
# ---------------------------------------------------------------------------

CLASSES = ["ok", "usage_error", "config_missing", "config_load_error",
           "connection_error", "traceback", "other_error"]

_RULES: list[tuple[str, str]] = [
    # (class, regex over combined output, first match wins)
    ("traceback", r"Traceback \(most recent call last\)|at Object\.|node:internal|panic:"),
    ("config_load_error", r"could not load its dependencies|cannot find \"@chkit|Error [A-Z_]*CONFIG"),
    ("config_missing", r"Config not found|no config file|clickhouse\.config\.(ts|py)"),
    ("connection_error", r"Could not connect|connection refused|ECONNREFUSED|ClickHouse at http"),
    ("usage_error", r"Usage:|unknown option|Unknown command|No such command|unexpected argument|Try '.*--help'"),
]


def classify(exit_code: int, out: str, err: str) -> str:
    """Map one run to its behavioural class. A run that succeeded is 'ok'
    unless it crashed its way there; error classes only apply to failures."""
    text = out + "\n" + err
    if re.search(_RULES[0][1], text):
        return "traceback"
    if exit_code == 0:
        return "ok"
    for cls, pat in _RULES[1:]:
        if re.search(pat, text):
            return cls
    return "other_error"


def normalize(text: str, workdir: str) -> str:
    """Strip run-specific noise before any content comparison."""
    text = text.replace(workdir, "<DIR>")
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)          # ANSI colours
    text = re.sub(r"0\.2\.0-beta\.\d+|0\.3\.\d+", "<VER>", text)
    return text


# ---------------------------------------------------------------------------
# Scenario execution
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class PortResult:
    port: str
    exit_code: int
    stdout: str
    stderr: str
    cls: str
    files: list[str]          # relative paths created by the scenario
    assertions_failed: list[str]


def run_scenario(scn: dict, port_name: str, cmd: str, base_env: dict) -> PortResult:
    """Run one scenario against one port in a fresh temp dir."""
    with tempfile.TemporaryDirectory(prefix=f"parity-{port_name}-") as td:
        for rel, content in (scn.get("files") or {}).items():
            p = Path(td, rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        before = {str(p.relative_to(td)) for p in Path(td).rglob("*")
                  if p.is_file() and "node_modules" not in p.parts}
        env = dict(base_env)
        env.update(scn.get("env") or {})
        timeout = scn.get("timeout_s", 60)
        # Optional setup steps run in the same directory first (e.g. init,
        # then the command under test against the scaffolded project).
        for pre in scn.get("pre") or []:
            subprocess.run(shlex.split(cmd) + pre, cwd=td, env=env,
                           capture_output=True, text=True, timeout=timeout)
        argv = shlex.split(cmd) + scn["argv"]
        try:
            proc = subprocess.run(argv, cwd=td, env=env, capture_output=True,
                                  text=True, timeout=timeout)
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            code, out, err = -1, "", "TIMEOUT"
        after = {str(p.relative_to(td)) for p in Path(td).rglob("*")
                 if p.is_file() and "node_modules" not in p.parts}
        new_files = sorted(after - before)
        failed = []
        for a in scn.get("assert") or []:
            stream = {"stdout": out, "stderr": err, "both": out + err}[a.get("stream", "both")]
            if a.get("contains") and a["contains"] not in stream:
                failed.append(f"{a.get('stream','both')} lacks {a['contains']!r}")
            if a.get("lacks") and a["lacks"] in stream:
                failed.append(f"{a.get('stream','both')} contains forbidden {a['lacks']!r}")
        return PortResult(port_name, code, normalize(out, td), normalize(err, td),
                          classify(code, out, err), new_files, failed)


def compare(scn: dict, ts: PortResult, py: PortResult) -> list[str]:
    """A divergence is a difference a user of 'the same CLI' would notice."""
    divs = []
    if ts.exit_code != py.exit_code:
        divs.append(f"exit code: ts={ts.exit_code} py={py.exit_code}")
    if ts.cls != py.cls:
        divs.append(f"class: ts={ts.cls} py={py.cls}")
    # init-style scenarios: the scaffolded file SET must match modulo language.
    if scn.get("compare_files"):
        norm = lambda fs: sorted(re.sub(r"\.(ts|py)$", ".<LANG>", f) for f in fs
                                 if f not in scn.get("ignore_files", []))
        if norm(ts.files) != norm(py.files):
            divs.append(f"files created: ts={ts.files} py={py.files}")
    for r in (ts, py):
        for f in r.assertions_failed:
            divs.append(f"{r.port}: assertion failed: {f}")
    return divs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", required=True, type=Path)
    ap.add_argument("--ts", required=True, help="shell command for the TypeScript CLI")
    ap.add_argument("--py", required=True, help="shell command for the Python CLI")
    ap.add_argument("--json-out", type=Path)
    args = ap.parse_args()

    base_env = {k: v for k, v in os.environ.items()
                if k not in ("CLICKHOUSE_URL", "CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD")}
    base_env["NO_COLOR"] = "1"
    base_env["CI"] = "1"  # both CLIs take non-interactive paths under CI

    files = sorted(args.scenarios.glob("*.json"))
    if not files:
        print(f"no scenarios under {args.scenarios}", file=sys.stderr)
        return 2

    report, n_div = [], 0
    for f in files:
        scn = json.loads(f.read_text())
        ts = run_scenario(scn, "ts", args.ts, base_env)
        py = run_scenario(scn, "py", args.py, base_env)
        divs = compare(scn, ts, py)
        n_div += bool(divs)
        mark = "DIVERGES" if divs else "parity  "
        print(f"{mark}  {scn['name']:<28} ts(exit={ts.exit_code},{ts.cls}) "
              f"py(exit={py.exit_code},{py.cls})")
        for d in divs:
            print(f"           - {d}")
        report.append({"scenario": scn["name"],
                       "ts": dataclasses.asdict(ts), "py": dataclasses.asdict(py),
                       "divergences": divs})
    print(f"\n{len(files)} scenarios, {n_div} diverging")
    if args.json_out:
        args.json_out.write_text(json.dumps(report, indent=2))
    return 1 if n_div else 0


if __name__ == "__main__":
    sys.exit(main())
