"""Проверяет границы повторного ревью на временных git-репозиториях."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "plugins/review-kit/scripts/review_snapshot.py"
SPEC = importlib.util.spec_from_file_location("review_snapshot", SCRIPT)
snapshots = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(snapshots)


class ReviewSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Review Test")
        self.write("code.txt", "base\n")
        self.write("AGENTS.md", "Project rules\n")
        self.commit("base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("switch", "-c", "feature")
        self.write("code.txt", "feature\n")
        self.commit("change")
        self.report = {
            "verdict": "READY AFTER FIXES", "findings": [{"title": 'Кавычки " и\nстрока'}],
            "open": ["Проверить границу доступа"], "checks": [{"command": "test", "result": "passed"}],
        }

    def git(self, *args):
        return snapshots.git(self.repo, *args).decode().strip()

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-m", message)

    def capture(self, **options):
        return snapshots.capture(self.repo, options.pop("base", self.base), **options)

    def record(self, **options):
        result = self.capture(**options)
        snapshots.record(self.repo, result["snapshot"], self.report)
        return result

    def test_same_snapshot_reuses_report_with_json_roundtrip(self):
        self.record()
        result = self.capture()
        self.assertEqual("unchanged", result["mode"])
        self.assertEqual(self.report, result["previous"]["report"])

    def test_new_commit_can_receive_followup(self):
        self.record()
        self.write("code.txt", "fix\n")
        self.commit("fix")
        self.assertEqual("followup", self.capture()["mode"])

    def test_moving_base_invalidates_same_head(self):
        self.record()
        self.git("switch", "main")
        self.write("release.txt", "new base\n")
        self.commit("release")
        moved_base = self.git("rev-parse", "HEAD")
        self.git("switch", "feature")
        self.assertEqual("first", self.capture(base=moved_base)["mode"])

    def test_explicit_release_base_is_used_instead_of_default_branch(self):
        self.git("switch", "-c", "release", self.base)
        self.write("release.txt", "release contract\n")
        self.commit("release base")
        release = self.git("rev-parse", "HEAD")
        self.git("switch", "feature")
        self.git("merge", "--no-edit", "release")
        result = self.capture(base=release)["snapshot"]
        self.assertEqual(release, result["base_sha"])
        self.assertEqual(release, result["merge_base"])

    def test_changed_dirty_content_invalidates_same_status(self):
        self.write("code.txt", "first pending fix\n")
        self.record()
        self.write("code.txt", "other pending fix\n")
        self.assertEqual("first", self.capture()["mode"])

    def test_untracked_content_with_unusual_name_is_part_of_snapshot(self):
        self.write('new "file\n.txt', "first\n")
        self.record()
        self.write('new "file\n.txt', "second\n")
        self.assertEqual("first", self.capture()["mode"])

    def test_changed_index_invalidates_same_worktree_and_status(self):
        self.write("code.txt", "staged version one\n")
        self.git("add", "code.txt")
        self.write("code.txt", "final worktree content\n")
        self.record()
        self.write("code.txt", "staged version two\n")
        self.git("add", "code.txt")
        self.write("code.txt", "final worktree content\n")
        self.assertEqual("first", self.capture()["mode"])

    def test_project_guide_invalidates_previous_result(self):
        self.write("guides/api.md", "first contract\n")
        self.commit("guide")
        self.record(rules=["guides/api.md"])
        self.write("guides/api.md", "new contract\n")
        self.commit("updated guide")
        self.assertEqual("first", self.capture(rules=["guides/api.md"])["mode"])

    def test_committed_project_rules_require_full_review(self):
        self.record()
        self.write("AGENTS.md", "Changed project rules\n")
        self.commit("updated rules")
        self.assertEqual("first", self.capture()["mode"])

    def test_updated_plugin_invalidates_cached_report(self):
        plugin = Path(self.temp.name) / "plugin"
        plugin.mkdir()
        instructions = plugin / "SKILL.md"
        instructions.write_text("first checklist\n")
        with patch.object(snapshots, "PLUGIN_ROOT", plugin):
            self.record()
            instructions.write_text("updated checklist\n")
            self.assertEqual("first", self.capture()["mode"])

    def test_broader_requested_scope_does_not_reuse_narrow_review(self):
        self.record(scope="min")
        self.assertEqual("first", self.capture(scope="all")["mode"])

    def test_rewritten_commit_with_same_message_requires_full_review(self):
        self.record()
        self.git("switch", "-c", "rewritten", self.base)
        self.write("code.txt", "different change\n")
        self.commit("change")
        self.assertEqual("first", self.capture(target="feature")["mode"])

    def test_record_rejects_changes_made_during_review(self):
        result = self.capture()
        self.write("code.txt", "changed while reviewing\n")
        with self.assertRaisesRegex(ValueError, "изменились"):
            snapshots.record(self.repo, result["snapshot"], self.report)
        self.assertFalse(snapshots.log_path(self.repo).exists())

    def test_worktrees_share_history_without_touching_original_changes(self):
        original = self.git("rev-parse", "HEAD")
        self.write("code.txt", "user work\n")
        worktree = Path(self.temp.name) / "review worktree"
        self.git("worktree", "add", "--detach", str(worktree), original)
        result = snapshots.capture(worktree, self.base, target="https://github.com/a/b/pull/1")
        snapshots.record(worktree, result["snapshot"], self.report)
        self.assertEqual(snapshots.log_path(self.repo), snapshots.log_path(worktree))
        self.assertEqual("user work\n", (self.repo / "code.txt").read_text())
        self.assertEqual("feature", self.git("branch", "--show-current"))

    def test_invalid_log_line_does_not_hide_completed_review(self):
        self.record()
        with snapshots.log_path(self.repo).open("a") as stream:
            stream.write('broken\nnull\n{"snapshot":null}\n')
        self.assertEqual("unchanged", self.capture()["mode"])

    def test_cli_capture_and_record(self):
        snapshot_file = Path(self.temp.name) / "snapshot.json"
        report_file = Path(self.temp.name) / "report.json"
        result = subprocess.run(
            ["python3", str(SCRIPT), "capture", "--repo", str(self.repo), "--base", self.base],
            check=True, capture_output=True, text=True,
        )
        snapshot_file.write_text(result.stdout)
        report_file.write_text(json.dumps(self.report))
        subprocess.run(
            ["python3", str(SCRIPT), "record", "--repo", str(self.repo),
             "--snapshot", str(snapshot_file), "--report", str(report_file)],
            check=True, capture_output=True,
        )
        self.assertEqual("unchanged", self.capture()["mode"])


if __name__ == "__main__":
    unittest.main()
