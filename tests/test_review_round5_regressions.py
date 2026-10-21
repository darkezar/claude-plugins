"""Частичный ремонт с явным карантином и без потери подтверждённых статусов."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/review-kit/scripts'
sys.path.insert(0, str(SCRIPTS))
import reply_map
from review_storage import read_json, write_json


class RoundFiveRegressionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.pr = 'https://github.com/owner/repo/pull/1'
        self.path = reply_map.map_path(self.repo, self.pr, create=True)
        self.candidate = self.root / 'candidate.json'
        self.item = {'comment_id': 'C1', 'thread_id': 'T1', 'state': 'local',
                     'resolved': False, 'reply_verified': False}

    def repair(self, payload, *args):
        write_json(self.candidate, payload)
        return subprocess.run([sys.executable, reply_map.__file__, 'repair', '--repo', str(self.repo),
            '--pr', self.pr, '--input', str(self.candidate), *args], capture_output=True, text=True, timeout=10)

    def payload(self, items):
        return {'version': 1, 'pr': self.pr, 'revision': 4, 'items': items}

    def test_unrecoverable_items_are_quarantined_without_blocking_other_replies(self):
        source = self.payload([{**self.item, 'thread_id': None, 'extra': 'keep'},
                               {'comment_id': 'DELETED', 'state': 'pending'}, {'state': 'pending'}])
        write_json(self.path, source)
        original = self.path.read_bytes()
        result = self.repair(self.payload([self.item]), '--quarantine-item', '1', '--quarantine-item', '2',
                             '--quarantine-reason', 'Не восстановлено по GitHub')
        self.assertEqual(0, result.returncode, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual([1, 2], [item['index'] for item in receipt['quarantined']])
        self.assertEqual(['DELETED', None], [item['comment_id'] for item in receipt['quarantined']])
        self.assertEqual(original, Path(receipt['backup']).read_bytes())
        current = reply_map.load_map(self.repo, self.pr)
        self.assertEqual(['C1'], [item['comment_id'] for item in current['items']])
        self.assertEqual('keep', current['items'][0]['extra'])
        self.assertEqual(receipt['quarantined'], current['repair_history'][-1]['quarantined'])
        current['items'][0]['fix'] = 'Safe replies can continue'
        reply_map.save_map(self.repo, self.pr, current)
        self.assertEqual(6, reply_map.load_map(self.repo, self.pr)['revision'])

    def test_quarantine_requires_explicit_reason_and_valid_source_positions(self):
        source = self.payload([{**self.item, 'thread_id': None}, {'state': 'pending'}])
        write_json(self.path, source)
        original = self.path.read_bytes()
        for args, code in [(('--quarantine-item', '1'), 2),
                           (('--quarantine-item', '9', '--quarantine-reason', 'Unknown'), 1),
                           ((), 1)]:
            with self.subTest(args=args):
                result = self.repair(self.payload([self.item]), *args)
                self.assertEqual(code, result.returncode, result.stderr)
                self.assertEqual(original, self.path.read_bytes())
        self.assertEqual([], list(self.path.parent.glob('*backup*')))

    def test_repair_cannot_reset_or_implicitly_inherit_published_status(self):
        source = self.payload([{**self.item, 'thread_id': None, 'state': 'published',
                                'resolved': True, 'reply_verified': True}])
        cases = [{**self.item, 'state': 'pending'},
                 {**self.item, 'state': 'published', 'reply_verified': True},
                 {**self.item, 'state': 'published', 'resolved': True},
                 {'comment_id': 'C1', 'thread_id': 'T1', 'state': 'published'}]
        for candidate in cases:
            with self.subTest(candidate=candidate):
                write_json(self.path, source)
                original = self.path.read_bytes()
                result = self.repair(self.payload([candidate]))
                self.assertEqual(1, result.returncode, result.stderr)
                self.assertEqual(original, self.path.read_bytes())
        confirmed = {**self.item, 'state': 'published', 'resolved': True, 'reply_verified': True}
        write_json(self.path, source)
        result = self.repair(self.payload([confirmed]))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(confirmed, reply_map.load_map(self.repo, self.pr)['items'][0])

    def test_missing_or_valid_map_and_invalid_write_json_fail_without_changes(self):
        result = self.repair(self.payload([self.item]))
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertFalse(self.path.exists())
        write_json(self.path, self.payload([self.item]))
        original = self.path.read_bytes()
        result = self.repair(self.payload([self.item]))
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertEqual(original, self.path.read_bytes())
        output = self.root / 'report.json'
        result = subprocess.run([sys.executable, '-I', str(SCRIPTS / 'review_storage.py'),
            'write', '--output', str(output)], input=b'{invalid', capture_output=True, timeout=10)
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertEqual(b'', result.stdout)
        self.assertFalse(output.exists())

    def test_reply_map_file_invocation_does_not_import_modules_from_pr_cwd(self):
        marker = self.root / 'poison'
        for module in ('json', 'tempfile'):
            (self.repo / (module + '.py')).write_text(f'open({str(marker)!r}, "w").write("bad")\n')
        env = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
        result = subprocess.run([sys.executable, reply_map.__file__, 'load', '--repo', str(self.repo),
            '--pr', self.pr], cwd=self.repo, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], json.loads(result.stdout)['items'])
        self.assertFalse(marker.exists())

    def test_report_input_recipe_creates_private_file_with_umask_022(self):
        reference = (SCRIPTS.parent / 'references/review-artifacts.md').read_text(encoding='utf-8')
        recipe = next(block.split('```', 1)[0] for block in reference.split('```bash\n')[1:]
                      if 'mktemp' in block.split('```', 1)[0] and 'REPORT_INPUT' in block.split('```', 1)[0])
        result = subprocess.run(['bash', '-c', recipe], cwd=self.repo, umask=0o022,
            env={**os.environ, 'ARTIFACT_DIR': str(self.root)}, capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        path = Path(result.stdout.strip())
        self.assertEqual(self.root.resolve(), path.parent.resolve())
        self.assertEqual(0o600, path.stat().st_mode & 0o777)


if __name__ == '__main__':
    unittest.main()
