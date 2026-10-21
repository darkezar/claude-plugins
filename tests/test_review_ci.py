"""CI не становится зелёным из-за пустого ответа, неверного SHA или неполных проверок."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "plugins/review-kit/scripts/review_ci.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("review_ci", SCRIPT)
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)


def job(name="tests", bucket="pass"):
    return {"name": name, "bucket": bucket, "state": bucket.upper(),
            "workflow": "CI", "link": f"https://example.invalid/jobs/{name}"}


class ReviewCITest(unittest.TestCase):
    def observe(self, checks, required, after=None, expected=()):
        before = {"headRefOid": "head", "baseRefOid": "base"}
        with patch.object(ci, "query", side_effect=[before, checks, required, after or before]):
            return ci.observation("owner/repo", 1, "head", "base", expected, float("inf"))

    def test_only_completed_required_checks_confirm_ci(self):
        result = self.observe([job(), job("optional", "pending")], [job()])
        self.assertEqual("pass", result["state"])
        compact = ci.summary(result, Path("ci.json"))
        self.assertEqual([job("optional", "pending")], compact["attention"])

    def test_empty_or_undiscovered_required_checks_are_not_success(self):
        for required in ([], ValueError("no required checks reported")):
            with self.subTest(required=required):
                result = self.observe([job()], required)
                self.assertEqual("unconfirmed", result["state"])

    def test_project_required_check_must_appear_and_finish(self):
        result = self.observe([job()], [], expected=("CI/tests", "CI/integration"))
        self.assertEqual("pending", result["state"])
        self.assertEqual(["CI/integration"], result["missing"])
        result = self.observe([job()], [], expected=("CI/tests",))
        self.assertEqual("pass", result["state"])
        result = self.observe([job()], ValueError("rules unavailable"), expected=("CI/tests",))
        self.assertEqual("unconfirmed", result["state"])

    def test_failed_cancelled_skipped_or_unknown_required_check_cannot_pass(self):
        for bucket, state in (("fail", "fail"), ("pending", "pending"),
                              ("cancel", "unconfirmed"), ("skipping", "unconfirmed"),
                              ("unknown", "unconfirmed")):
            with self.subTest(bucket=bucket):
                self.assertEqual(state, self.observe([job(bucket=bucket)], [job(bucket=bucket)])["state"])

    def test_unrelated_failures_are_kept_for_the_reviewer(self):
        result = self.observe([job(), job("optional", "fail")], [job()])
        self.assertEqual("pass", result["state"])
        self.assertEqual([job("optional", "fail")], ci.summary(result, Path("ci.json"))["attention"])

    def test_old_head_is_rejected_before_checks(self):
        with patch.object(ci, "query", return_value={"headRefOid": "other", "baseRefOid": "base"}) as read:
            result = ci.observation("owner/repo", 1, "head", "base", (), float("inf"))
        self.assertEqual("stale", result["state"])
        self.assertEqual(1, read.call_count)

    def test_success_is_invalidated_if_head_moves_during_read(self):
        after = {"headRefOid": "other", "baseRefOid": "base"}
        self.assertEqual("stale", self.observe([job()], [job()], after=after)["state"])

    def test_wait_polls_in_the_script_and_stops_after_success(self):
        pending = {"state": "pending", "checks": [], "required": []}
        passed = {"state": "pass", "checks": [job()], "required": [job()]}
        with patch.object(ci, "observation", side_effect=[pending, passed]) as read, \
                patch.object(ci.time, "sleep") as sleep:
            result = ci.collect("owner/repo", 1, "head", "base", wait_seconds=60)
        self.assertEqual("pass", result["state"])
        self.assertEqual(2, read.call_count)
        sleep.assert_called_once_with(15)

    def test_pending_is_returned_when_wait_budget_cannot_fit_another_poll(self):
        pending = {"state": "pending", "reason": "waiting", "checks": [], "required": []}
        with patch.object(ci, "observation", return_value=pending) as read, \
                patch.object(ci.time, "sleep") as sleep:
            result = ci.collect("owner/repo", 1, "head", "base", wait_seconds=1)
        self.assertEqual("pending", result["state"])
        self.assertEqual(1, read.call_count)
        sleep.assert_not_called()

    def test_api_or_timeout_failure_is_unconfirmed(self):
        for error in (ValueError("403"), OSError("gh missing"),
                      subprocess.TimeoutExpired("gh", 1)):
            with self.subTest(error=error), patch.object(ci, "observation", side_effect=error):
                self.assertEqual("unconfirmed", ci.collect("owner/repo", 1, "head", "base")["state"])

    def test_gh_exit_codes_for_checks_do_not_hide_json(self):
        for code in (0, 1, 8):
            completed = subprocess.CompletedProcess([], code, stdout='[{"bucket":"pending"}]', stderr="")
            with self.subTest(code=code), patch.object(ci.subprocess, "run", return_value=completed):
                self.assertEqual([{"bucket": "pending"}], ci.query([], float("inf")))

    def test_cli_usage_and_output_errors_are_distinct_from_ci_unconfirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad_output = root / "link.json"
            target = root / "target.json"
            target.write_text("untouched")
            bad_output.symlink_to(target)
            # Некорректный формат обязательной job не должен обращаться к gh.
            cases = [[], ["--repo", "owner/repo", "--pr", "1", "--head", "head",
                           "--base", "base", "--output", str(bad_output),
                           "--expect-check", "tests"]]
            for args in cases:
                result = subprocess.run([sys.executable, str(SCRIPT), *args],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(4, result.returncode)
                self.assertEqual("", result.stdout)
            with patch.object(ci, "collect", return_value={"state": "pass"}), \
                    patch.object(sys, "argv", [str(SCRIPT), "--repo", "owner/repo", "--pr", "1",
                                              "--head", "head", "--base", "base", "--output", str(bad_output)]):
                with self.assertRaises(SystemExit) as stopped:
                    ci.main()
                self.assertEqual(4, stopped.exception.code)
            self.assertEqual("untouched", target.read_text())

    def test_cli_exit_status_and_artifact_with_a_fake_gh(self):
        # Проверяем реальную границу CLI, не обращаясь к GitHub или пользовательскому gh.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gh = root / "gh"
            gh.write_text('''#!/usr/bin/env python3
import json, os, sys
case = os.environ["REVIEW_CI_TEST_CASE"]
if sys.argv[1:3] == ["pr", "view"]:
    print(json.dumps({"headRefOid": "other" if case == "stale" else "head", "baseRefOid": "base"}))
else:
    if case == "unconfirmed":
        print("no required checks reported on the 'feature' branch" if "--required" in sys.argv else "no checks reported on the 'feature' branch", file=sys.stderr)
        sys.exit(1)
    checks = [{"name": "tests", "bucket": case, "state": case.upper()}]
    print(json.dumps(checks))
    sys.exit({"fail": 1, "pending": 8}.get(case, 0))
''')
            gh.chmod(0o755)
            for state, code in (("pass", 0), ("fail", 1), ("pending", 8),
                                ("unconfirmed", 2), ("stale", 3)):
                with self.subTest(state=state):
                    output = root / f"{state}.json"
                    env = {**os.environ, "PATH": f"{root}{os.pathsep}{os.environ['PATH']}",
                           "REVIEW_CI_TEST_CASE": state}
                    result = subprocess.run(
                        [sys.executable, str(SCRIPT), "--repo", "owner/repo", "--pr", "1",
                         "--head", "head", "--base", "base", "--output", str(output)],
                        capture_output=True, text=True, env=env, timeout=10,
                    )
                    self.assertEqual(code, result.returncode, result.stderr)
                    compact, full = json.loads(result.stdout), json.loads(output.read_text())
                    self.assertEqual(state, compact["state"])
                    self.assertEqual(state, full["state"])
                    self.assertEqual("observed_pr_metadata_only", compact["base_evidence"])
                    self.assertEqual(str(output), compact["artifact"])
                    self.assertNotIn("checks", compact)
                    self.assertIn("checks", full)


if __name__ == "__main__":
    unittest.main()
