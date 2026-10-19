"""Проверяет границы повторного ревью на временных git-репозиториях."""

import importlib.util
import json
import os
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
        self.codex_home = Path(self.temp.name) / "codex-home"
        self.codex_home.mkdir()
        environment = patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)})
        environment.start()
        self.addCleanup(environment.stop)
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

    def move_base(self, path="release.txt", text="new base\n"):
        self.git("switch", "main")
        self.write(path, text)
        self.commit("release")
        moved = self.git("rev-parse", "HEAD")
        self.git("switch", "feature")
        return moved

    def test_moved_base_with_same_branch_is_followup_without_increment(self):
        self.record()
        result = self.capture(base=self.move_base())
        self.assertEqual("followup", result["mode"])
        self.assertEqual({"commits": [], "merges": [], "files": []}, result["increment"])

    def test_fix_after_base_moved_is_followup(self):
        # GitHub сдвигает базу PR при каждом push, поэтому сдвиг базы не должен сбрасывать
        # ветку на полный прогон.
        self.record()
        moved = self.move_base()
        self.write("code.txt", "fix\n")
        self.commit("fix")
        result = self.capture(base=moved)
        self.assertEqual("followup", result["mode"])
        self.assertEqual([self.git("rev-parse", "HEAD")], result["increment"]["commits"])
        self.assertEqual(["code.txt"], result["increment"]["files"])

    def test_merged_base_is_not_reviewed_but_conflict_resolution_is(self):
        self.record()
        self.git("switch", "main")
        self.write("code.txt", "main version\n")
        self.write("release.txt", "from main\n")
        self.commit("release")
        moved = self.git("rev-parse", "HEAD")
        self.git("switch", "feature")
        with self.assertRaises(subprocess.CalledProcessError):
            self.git("merge", "main")
        self.write("code.txt", "resolved\n")
        self.git("add", "code.txt")
        self.git("commit", "--no-edit")
        result = self.capture(base=moved)
        self.assertEqual("followup", result["mode"])
        self.assertEqual([], result["increment"]["commits"])
        self.assertEqual(["code.txt"], result["increment"]["files"])

    def test_rebased_branch_requires_full_review(self):
        self.record()
        moved = self.move_base()
        self.git("rebase", moved)
        self.assertEqual("first", self.capture(base=moved)["mode"])

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

    def test_merge_resolution_matching_either_parent_is_reviewed(self):
        for choice in ("--ours", "--theirs"):
            with self.subTest(choice=choice):
                case = ReviewSnapshotTest()
                case.setUp()
                try:
                    case.record()
                    moved = case.move_base("code.txt", "main version\n")
                    with self.assertRaises(subprocess.CalledProcessError):
                        case.git("merge", "main")
                    case.git("checkout", choice, "--", "code.txt")
                    case.git("add", "code.txt")
                    case.git("commit", "--no-edit")
                    result = case.capture(base=moved)
                    self.assertEqual("followup", result["mode"])
                    self.assertEqual([], result["increment"]["commits"])
                    self.assertEqual(["code.txt"], result["increment"]["files"])
                finally:
                    case.doCleanups()

    def test_conflict_free_merge_does_not_review_imported_base_files(self):
        self.record()
        moved = self.move_base()
        self.git("merge", "--no-edit", "main")
        result = self.capture(base=moved)
        self.assertEqual("followup", result["mode"])
        self.assertEqual([], result["increment"]["files"])

    def test_old_git_fallback_keeps_merge_resolutions(self):
        self.record()
        moved = self.move_base("code.txt", "main version\n")
        with self.assertRaises(subprocess.CalledProcessError):
            self.git("merge", "main")
        self.git("checkout", "--ours", "--", "code.txt")
        self.git("add", "code.txt")
        self.git("commit", "--no-edit")
        original_git = snapshots.git

        def old_git(repo, *args):
            if "--remerge-diff" in args:
                raise subprocess.CalledProcessError(129, ["git", *args])
            return original_git(repo, *args)

        with patch.object(snapshots, "git", old_git):
            self.assertEqual(["code.txt"], self.capture(base=moved)["increment"]["files"])

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

    def fake_plugin(self):
        plugin = Path(self.temp.name) / "plugin"
        for path in ("agents/agent-branch-reviewer.md", ".codex-plugin/plugin.json"):
            (plugin / path).parent.mkdir(parents=True, exist_ok=True)
            (plugin / path).write_text("first\n")
        return plugin

    def test_updated_checklist_invalidates_cached_report(self):
        plugin = self.fake_plugin()
        with patch.object(snapshots, "PLUGIN_ROOT", plugin):
            self.record()
            (plugin / "agents/agent-branch-reviewer.md").write_text("updated checklist\n")
            self.assertEqual("first", self.capture()["mode"])

    def test_plugin_release_without_checklist_changes_keeps_result(self):
        plugin = self.fake_plugin()
        with patch.object(snapshots, "PLUGIN_ROOT", plugin):
            self.record()
            (plugin / ".codex-plugin/plugin.json").write_text("new version\n")
            self.assertEqual("unchanged", self.capture()["mode"])

    def test_codex_context_changes_invalidate_cached_review(self):
        plugin = self.fake_plugin()
        paths = [plugin / path / "SKILL.md" for path in (
            "codex-skills/detect-stack", "codex-skills/stack-nextjs",
            "codex-skills/i18n-sync", "skills/detect-stack", "skills/i18n-sync"
        )]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("initial context\n")
        with patch.object(snapshots, "PLUGIN_ROOT", plugin):
            for path in paths:
                with self.subTest(skill=str(path.relative_to(plugin))):
                    self.record()
                    path.write_text("updated review context\n")
                    self.assertEqual("first", self.capture()["mode"])

    def test_change_hidden_by_textconv_invalidates_snapshot(self):
        self.write(".gitattributes", "*.dat diff=first-line\n")
        self.git("config", "diff.first-line.textconv", "head -n1")
        self.write("data.dat", "visible\nhidden one\n")
        self.commit("data")
        self.write("data.dat", "visible\nhidden two\n")
        self.record()
        self.write("data.dat", "visible\nhidden three\n")
        self.assertEqual("first", self.capture()["mode"])

    def test_external_diff_does_not_hide_changes(self):
        self.git("config", "diff.external", "true")
        self.write("code.txt", "first pending fix\n")
        self.record()
        self.write("code.txt", "other pending fix\n")
        self.assertEqual("first", self.capture()["mode"])

    def test_changed_content_inside_submodule_invalidates_snapshot(self):
        library = Path(self.temp.name) / "library"
        library.mkdir()
        snapshots.git(library, "init", "-b", "main")
        snapshots.git(library, "config", "user.email", "test@example.invalid")
        snapshots.git(library, "config", "user.name", "Review Test")
        (library / "lib.txt").write_text("library\n")
        snapshots.git(library, "add", ".")
        snapshots.git(library, "commit", "-m", "library")
        self.git("-c", "protocol.file.allow=always", "submodule", "add", str(library), "lib")
        self.commit("submodule")
        (self.repo / "lib/lib.txt").write_text("first local change\n")
        self.record()
        (self.repo / "lib/lib.txt").write_text("second local change\n")
        self.assertEqual("first", self.capture()["mode"])

    def test_ignored_override_rules_invalidate_result(self):
        self.write(".gitignore", "AGENTS.override.md\n")
        self.commit("ignore local rules")
        self.write("AGENTS.override.md", "local rules\n")
        self.record()
        self.write("AGENTS.override.md", "changed local rules\n")
        self.assertEqual("first", self.capture()["mode"])

    def test_global_codex_rules_invalidate_result(self):
        (self.codex_home / "AGENTS.md").write_text("global rules\n")
        self.record()
        (self.codex_home / "AGENTS.md").write_text("changed global rules\n")
        self.assertEqual("first", self.capture()["mode"])

    def test_rules_outside_repository_are_accepted(self):
        guide = Path(self.temp.name) / "team-guide.md"
        guide.write_text("team contract\n")
        self.record(rules=[str(guide)])
        guide.write_text("new team contract\n")
        self.assertEqual("first", self.capture(rules=[str(guide)])["mode"])

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
