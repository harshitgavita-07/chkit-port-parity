#!/usr/bin/env python3
"""Unit tests for the parity harness: classifier, comparison, and a full
end-to-end run against two fake 'ports' (shell scripts) that proves the
harness both detects a planted divergence and passes identical ports."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import harness


class TestClassify(unittest.TestCase):
    def test_success_is_ok(self):
        self.assertEqual(harness.classify(0, "Usage: chkit <command>", ""), "ok")

    def test_usage_error_needs_failure(self):
        # help text mentions Usage: but exits 0 - must not be an error class
        self.assertEqual(harness.classify(0, "Usage: chkit", ""), "ok")
        self.assertEqual(harness.classify(2, "", "Usage: chkit\nunknown option --x"), "usage_error")

    def test_config_classes(self):
        self.assertEqual(harness.classify(1, "", "Config not found at /p/clickhouse.config.ts"),
                         "config_missing")
        self.assertEqual(harness.classify(1, "", "could not load its dependencies: cannot find \"@chkit/core\""),
                         "config_load_error")

    def test_connection_and_traceback(self):
        self.assertEqual(harness.classify(1, "", "Could not connect to ClickHouse (connection refused)"),
                         "connection_error")
        self.assertEqual(harness.classify(1, "", "Traceback (most recent call last):"), "traceback")
        # a crash beats a success exit code
        self.assertEqual(harness.classify(0, "", "Traceback (most recent call last):"), "traceback")

    def test_unknown_failure(self):
        self.assertEqual(harness.classify(1, "", "something odd"), "other_error")


class TestCompare(unittest.TestCase):
    def _res(self, port, code, cls, files=None, failed=None):
        return harness.PortResult(port, code, "", "", cls, files or [], failed or [])

    def test_exit_mismatch_diverges(self):
        d = harness.compare({"name": "x"}, self._res("ts", 1, "config_missing"),
                            self._res("py", 2, "config_missing"))
        self.assertTrue(any("exit code" in x for x in d))

    def test_class_mismatch_diverges(self):
        d = harness.compare({"name": "x"}, self._res("ts", 1, "config_missing"),
                            self._res("py", 1, "usage_error"))
        self.assertTrue(any("class" in x for x in d))

    def test_language_extension_normalised(self):
        scn = {"name": "init", "compare_files": True}
        d = harness.compare(scn, self._res("ts", 0, "ok", ["clickhouse.config.ts"]),
                            self._res("py", 0, "ok", ["clickhouse.config.py"]))
        self.assertEqual(d, [])

    def test_ignored_files(self):
        scn = {"name": "init", "compare_files": True, "ignore_files": ["package.json"]}
        d = harness.compare(scn, self._res("ts", 0, "ok", ["package.json", "a.ts"]),
                            self._res("py", 0, "ok", ["a.py"]))
        self.assertEqual(d, [])

    def test_assertion_failure_reported_per_port(self):
        d = harness.compare({"name": "x"}, self._res("ts", 0, "ok", failed=["stderr lacks 'init'"]),
                            self._res("py", 0, "ok"))
        self.assertEqual(len(d), 1)
        self.assertIn("ts", d[0])


FAKE_OK = "#!/bin/sh\necho 'Usage: fake <command>'\nexit 0\n"
FAKE_USAGE_ERR = "#!/bin/sh\necho 'Usage: fake <command>' >&2\nexit 2\n"


class TestEndToEnd(unittest.TestCase):
    def _run(self, ts_script, py_script):
        with tempfile.TemporaryDirectory() as td:
            scn_dir = Path(td, "scn")
            scn_dir.mkdir()
            (scn_dir / "s.json").write_text(json.dumps(
                {"name": "probe", "argv": [], "assert": [{"stream": "both", "contains": "fake"}]}))
            for name, script in (("ts", ts_script), ("py", py_script)):
                p = Path(td, name)
                p.write_text(script)
                p.chmod(p.stat().st_mode | stat.S_IXUSR)
            return subprocess.run(
                [sys.executable, str(Path(__file__).parent / "harness.py"),
                 "--scenarios", str(scn_dir), "--ts", str(Path(td, "ts")), "--py", str(Path(td, "py"))],
                capture_output=True, text=True)

    def test_identical_ports_pass(self):
        r = self._run(FAKE_OK, FAKE_OK)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("parity", r.stdout)

    def test_divergence_detected(self):
        r = self._run(FAKE_OK, FAKE_USAGE_ERR)
        self.assertEqual(r.returncode, 1)
        self.assertIn("DIVERGES", r.stdout)
        self.assertIn("exit code", r.stdout)


if __name__ == "__main__":
    unittest.main()
