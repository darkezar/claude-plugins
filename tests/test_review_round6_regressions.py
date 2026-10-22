"""Старые повреждённые карты, неизменяемый аудит и безопасный запуск CLI."""

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/review-kit/scripts'
sys.path.insert(0, str(SCRIPTS))
import reply_map
from review_storage import read_json, write_json


class RoundSixRegressionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.pr = 'https://github.com/owner/repo/pull/1'
        self.path = reply_map.map_path(self.repo, self.pr, create=True)
        self.input = self.root / 'input.json'
        self.item = {'comment_id': 'C1', 'thread_id': 'T1', 'state': 'pending',
                     'resolved': False, 'reply_verified': False}

    def payload(self, items):
        return {'version': 1, 'pr': self.pr, 'revision': 4, 'items': items}

    def invoke(self, command, payload, *args):
        write_json(self.input, payload)
        return subprocess.run([sys.executable, '-E', '-s', str(SCRIPTS / 'reply_map.py'), command,
            '--repo', str(self.repo), '--pr', self.pr, '--input', str(self.input), *args],
            capture_output=True, text=True, timeout=10)

    def test_numeric_quarantine_keeps_identity_and_complete_original_item(self):
        source_item = {**self.item, 'comment_id': 123, 'state': 'published', 'resolved': True,
                       'reply_verified': True, 'reply_id': 'REPLY', 'extra': {'keep': 'all'}}
        write_json(self.path, self.payload([source_item]))
        original = self.path.read_bytes()
        result = self.invoke('repair', self.payload([]), '--quarantine-item', '0',
                             '--quarantine-reason', 'Нельзя подтвердить ID')
        self.assertEqual(0, result.returncode, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual('123', receipt['quarantined'][0]['comment_id'])
        self.assertEqual(source_item, receipt['quarantined'][0]['source_item'])
        self.assertEqual(original, Path(receipt['backup']).read_bytes())
        self.assertEqual(receipt['quarantined'], read_json(self.path)['repair_history'][-1]['quarantined'])

    def test_quarantined_numeric_identity_cannot_return_as_string(self):
        source = self.payload([{**self.item, 'comment_id': 123, 'state': 'published',
                                'resolved': True, 'reply_verified': True}])
        write_json(self.path, source)
        original = self.path.read_bytes()
        result = self.invoke('repair', self.payload([{**self.item, 'comment_id': '123'}]),
                             '--quarantine-item', '0', '--quarantine-reason', 'Legacy ID')
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertEqual(original, self.path.read_bytes())
        self.assertEqual([], list(self.path.parent.glob('*backup*')))

    def test_quarantined_alias_cannot_bypass_status_guard_via_duplicate(self):
        source = self.payload([{**self.item, 'comment_id': 123, 'state': 'published'},
                               {**self.item, 'comment_id': '123'}])
        write_json(self.path, source)
        original = self.path.read_bytes()
        result = self.invoke('repair', self.payload([{**self.item, 'comment_id': '123'}]),
                             '--quarantine-item', '0', '--quarantine-reason', 'Duplicate legacy ID')
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertEqual(original, self.path.read_bytes())

    def test_invalid_legacy_status_requires_quarantine_and_is_preserved(self):
        cases = [{key: value} for key in ('resolved', 'reply_verified') for value in ('true', 1)]
        cases += [{'state': 'Published'}, {'state': 'PUBLISHED'}]
        for fields in cases:
            with self.subTest(fields=fields):
                old = {**self.item, **fields}
                write_json(self.path, self.payload([old]))
                original = self.path.read_bytes()
                result = self.invoke('repair', self.payload([self.item]))
                self.assertEqual(1, result.returncode, result.stderr)
                self.assertEqual(original, self.path.read_bytes())
                result = self.invoke('repair', self.payload([]), '--quarantine-item', '0',
                                     '--quarantine-reason', 'Статус не подтверждён')
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(old, json.loads(result.stdout)['quarantined'][0]['source_item'])

    def test_save_preserves_current_audit_and_rejects_injected_history(self):
        history = [{'revision': 4, 'backup': 'original', 'quarantined': [{'index': 0}]}]
        for incoming in (None, [], [{'revision': 999, 'backup': 'forged'}]):
            with self.subTest(incoming=incoming):
                write_json(self.path, {**self.payload([self.item]), 'repair_history': history})
                candidate = self.payload([{**self.item, 'fix': 'Updated'}])
                if incoming is not None:
                    candidate['repair_history'] = incoming
                result = self.invoke('save', candidate)
                self.assertEqual(0, result.returncode, result.stderr)
                saved = read_json(self.path)
                self.assertEqual(history, saved['repair_history'])
                self.assertEqual('Updated', saved['items'][0]['fix'])
        write_json(self.path, self.payload([self.item]))
        result = self.invoke('save', {**self.payload([self.item]), 'repair_history': history})
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn('repair_history', read_json(self.path))

    def test_negative_or_duplicate_quarantine_index_is_usage_error(self):
        write_json(self.path, self.payload([{**self.item, 'thread_id': None}]))
        original = self.path.read_bytes()
        for indices in (['-1'], ['0', '0']):
            with self.subTest(indices=indices):
                args = [part for index in indices for part in ('--quarantine-item', index)]
                result = self.invoke('repair', self.payload([self.item]), *args,
                                     '--quarantine-reason', 'Invalid indices')
                self.assertEqual(2, result.returncode, result.stderr)
                self.assertEqual('', result.stdout)
                self.assertEqual(original, self.path.read_bytes())
        self.assertEqual([], list(self.path.parent.glob('*backup*')))

    def test_documented_cli_ignores_pythonpath_and_user_site(self):
        reference = (SCRIPTS.parent / 'references/fix-review-threads.md').read_text(encoding='utf-8')
        recipe = re.search(r'До правок.*?через `(.*?)`', reference, re.S).group(1)
        substitutions = {'$PLUGIN_ROOT': str(SCRIPTS.parent), '$REPO_DIR': str(self.repo), '$PR_URL': self.pr}
        for variable, value in substitutions.items():
            recipe = recipe.replace(variable, value)
        marker = self.root / 'poison'
        poison = f'open({str(marker)!r}, "w").write("executed")\n'
        (self.repo / 'argparse.py').write_text(poison + 'raise RuntimeError("PR import")\n')
        userbase = self.root / 'userbase'
        site = userbase / f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'
        site.mkdir(parents=True)
        (site / 'poison.pth').write_text('import pathlib; pathlib.Path(' + repr(str(marker)) + ').touch()\n')
        env = {**os.environ, 'PYTHONPATH': ':' + str(self.repo) + ':', 'PYTHONUSERBASE': str(userbase)}
        result = subprocess.run(shlex.split(recipe), cwd=self.repo, env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual([], json.loads(result.stdout)['items'])


if __name__ == '__main__':
    unittest.main()
