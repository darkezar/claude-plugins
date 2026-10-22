"""Карты ответов переживают смену worktree, не принимают чужой PR и симлинки."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/review-kit/scripts'))
import reply_map
from review_storage import read_json, write_json


class ReviewStorageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.git('init', '-b', 'main', str(self.repo), cwd=self.root)
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        self.git('commit', '--allow-empty', '-m', 'base')
        self.pr = 'https://github.com/owner/repo/pull/1'
        self.data = {'version': 1, 'pr': self.pr, 'items': [{'comment_id': 'C1', 'thread_id': 'T1', 'state': 'local'}]}

    def git(self, *args, cwd=None):
        return subprocess.run(['git', '-C', str(cwd or self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_map_is_found_from_another_worktree_and_a_fresh_cli(self):
        path = reply_map.save_map(self.repo, self.pr, self.data)
        worktree = self.root / 'worktree'
        self.git('worktree', 'add', '--detach', str(worktree))
        self.assertEqual(path, reply_map.map_path(worktree, self.pr))
        result = subprocess.run([sys.executable, reply_map.__file__, 'load', '--repo', str(worktree),
                                 '--pr', self.pr], check=True, capture_output=True, text=True)
        self.assertEqual(self.data, json.loads(result.stdout))
        self.assertEqual(0o700, path.parent.stat().st_mode & 0o777)
        self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_missing_map_is_empty_and_does_not_create_directories(self):
        self.assertEqual([], reply_map.load_map(self.repo, self.pr)['items'])
        self.assertFalse(reply_map.map_path(self.repo, self.pr).parent.exists())

    def test_other_pr_cannot_be_saved_or_loaded_under_this_identity(self):
        with self.assertRaises(ValueError):
            reply_map.save_map(self.repo, self.pr + '2', self.data)
        path = reply_map.save_map(self.repo, self.pr, self.data)
        write_json(path, {**self.data, 'pr': self.pr + '2'})
        with self.assertRaises(ValueError):
            reply_map.load_map(self.repo, self.pr)

    def test_symlink_map_cannot_be_read_or_overwritten(self):
        path = reply_map.save_map(self.repo, self.pr, self.data)
        target = self.root / 'target.json'
        target.write_text('unchanged')
        path.unlink()
        path.symlink_to(target)
        for action in (lambda: reply_map.load_map(self.repo, self.pr),
                       lambda: reply_map.save_map(self.repo, self.pr, self.data)):
            with self.assertRaises(ValueError):
                action()
        self.assertEqual('unchanged', target.read_text())

    def test_public_directory_or_file_is_rejected(self):
        public = self.root / 'public'
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        with self.assertRaises(ValueError):
            write_json(public / 'ci.json', {})
        private = self.root / 'private'
        private.mkdir(mode=0o700)
        path = private / 'ci.json'
        write_json(path, {})
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            read_json(path)
        with self.assertRaises(ValueError):
            write_json(path, {})

    def test_symlink_directory_is_rejected(self):
        actual = self.root / 'actual'
        actual.mkdir(mode=0o700)
        path = reply_map.map_path(self.repo, self.pr)
        path.parent.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(ValueError):
            reply_map.save_map(self.repo, self.pr, self.data)


if __name__ == '__main__':
    unittest.main()
