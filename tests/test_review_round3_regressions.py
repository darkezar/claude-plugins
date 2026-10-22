"""Публичные CLI-контракты приватных снимков, карты ответов и имён CI jobs."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/review-kit/scripts'))
import reply_map
import review_ci
from review_storage import read_json, write_json


class RoundThreeRegressionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.pr = 'https://github.com/owner/repo/pull/1'

    def test_save_reports_revision_and_conflict_has_its_own_exit_code(self):
        source = self.root / 'map.json'
        write_json(source, {'version': 1, 'pr': self.pr, 'items': [], 'revision': 0})
        command = [sys.executable, reply_map.__file__, 'save', '--repo', str(self.repo),
                   '--pr', self.pr, '--input', str(source)]
        saved = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(0, saved.returncode, saved.stderr)
        self.assertEqual({'path': str(reply_map.map_path(self.repo, self.pr)), 'revision': 1},
                         json.loads(saved.stdout))
        conflict = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(3, conflict.returncode, conflict.stderr)
        self.assertEqual('', conflict.stdout)
        self.assertEqual(1, reply_map.load_map(self.repo, self.pr)['revision'])
        write_json(source, {'version': 1, 'pr': self.pr, 'items': [{}], 'revision': 1})
        invalid = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(1, invalid.returncode, invalid.stderr)
        self.assertEqual('', invalid.stdout)

    def test_explicit_ci_pair_matches_slashes_in_workflow_and_job(self):
        gh = self.root / 'gh'
        gh.write_text('''#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:3] == ['pr', 'view']:
    print(json.dumps({'headRefOid': 'head', 'baseRefOid': 'base'}))
elif '--required' in sys.argv:
    print('[]')
else:
    print(json.dumps([{'workflow': os.environ['PAIR_TEST_WORKFLOW'],
        'name': 'build (linux/amd64)', 'bucket': 'pass', 'state': 'SUCCESS'}]))
''', encoding='utf-8')
        gh.chmod(0o755)
        for workflow, expected_code, expected_state in [('CI/Backend', 0, 'pass'),
                                                       ('CI/Untrusted', 8, 'pending')]:
            with self.subTest(workflow=workflow):
                output = self.root / (expected_state + '.json')
                env = {**os.environ, 'PATH': str(self.root) + os.pathsep + os.environ['PATH'],
                       'PAIR_TEST_WORKFLOW': workflow}
                result = subprocess.run([sys.executable, review_ci.__file__, '--repo', 'owner/repo',
                    '--pr', '1', '--head', 'head', '--base', 'base', '--output', str(output),
                    '--expect-check-pair', 'CI/Backend', 'build (linux/amd64)'],
                    env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(expected_code, result.returncode, result.stderr)
                self.assertEqual(expected_state, read_json(output)['state'])

    def test_ambiguous_legacy_ci_name_is_rejected_before_querying_gh(self):
        result = subprocess.run([sys.executable, review_ci.__file__, '--repo', 'owner/repo',
            '--pr', '1', '--head', 'head', '--base', 'base', '--output', str(self.root / 'ci.json'),
            '--expect-check', 'CI/build (linux/amd64)'],
            env={**os.environ, 'PATH': str(self.root)}, capture_output=True, text=True, timeout=10)
        self.assertEqual(4, result.returncode, result.stderr)
        self.assertIn('--expect-check-pair', result.stderr)
        self.assertEqual('', result.stdout)
        self.assertFalse((self.root / 'ci.json').exists())


if __name__ == '__main__':
    unittest.main()
