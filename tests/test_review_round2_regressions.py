"""Поведение артефактов, конкурентных записей и идентичности jobs после второго ревью."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/review-kit/scripts'))
import reply_map
import review_ci
import review_storage


class RoundTwoRegressionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.pr = 'https://github.com/owner/repo/pull/1'
        self.item = {'comment_id': 'C1', 'thread_id': 'T1', 'state': 'local'}
        self.data = {'version': 1, 'pr': self.pr, 'items': [self.item]}

    def test_stale_writer_cannot_erase_another_sessions_items(self):
        reply_map.save_map(self.repo, self.pr, self.data)
        first = reply_map.load_map(self.repo, self.pr)
        stale = reply_map.load_map(self.repo, self.pr)
        first['items'].append({**self.item, 'comment_id': 'C2'})
        reply_map.save_map(self.repo, self.pr, first)
        stale['items'].append({**self.item, 'comment_id': 'C3'})
        with self.assertRaises(ValueError):
            reply_map.save_map(self.repo, self.pr, stale)
        self.assertEqual({'C1', 'C2'}, {i['comment_id'] for i in reply_map.load_map(self.repo, self.pr)['items']})

    def test_two_processes_cannot_both_commit_the_same_revision(self):
        reply_map.save_map(self.repo, self.pr, self.data)
        program = '''import json, sys
sys.path.insert(0, sys.argv[1])
import reply_map
repo, pr, comment = sys.argv[2:]
state = reply_map.load_map(repo, pr)
state['items'].append({'comment_id': comment, 'thread_id': 'T2', 'state': 'local'})
print('ready', flush=True)
sys.stdin.readline()
try:
    reply_map.save_map(repo, pr, state)
except ValueError:
    sys.exit(3)
'''
        processes = [subprocess.Popen([sys.executable, '-c', program, str(Path(reply_map.__file__).parent),
                                       str(self.repo), self.pr, comment], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for comment in ('C2', 'C3')]
        try:
            for process in processes:
                self.assertEqual('ready', process.stdout.readline().strip())
            for process in processes:
                process.stdin.write('save\n')
                process.stdin.flush()
            results = [process.communicate(timeout=10) for process in processes]
            self.assertEqual([0, 3], sorted(p.returncode for p in processes), results)
            current = reply_map.load_map(self.repo, self.pr)
            ids = {i['comment_id'] for i in current['items']}
            self.assertEqual(2, len(ids))
            missing = ({'C2', 'C3'} - ids).pop()
            current['items'].append({**self.item, 'comment_id': missing})
            reply_map.save_map(self.repo, self.pr, current)
            self.assertEqual(3, len(reply_map.load_map(self.repo, self.pr)['items']))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_item_structure_and_comment_url_are_bound_to_pr(self):
        invalid = [[], {'comment_id': 'C1'}, {**self.item, 'thread_id': None},
                   {**self.item, 'state': 'anything'}, {**self.item, 'resolved': 'yes'},
                   {**self.item, 'source_body': {'command': 'execute'}},
                   {**self.item, 'comment_url': self.pr + '2#discussion_r1'}]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError):
                reply_map.validate({**self.data, 'items': [item]}, self.pr)
        with self.assertRaises(ValueError):
            reply_map.validate({**self.data, 'items': [self.item, self.item]}, self.pr)

    def test_utf8_json_is_independent_of_ascii_locale(self):
        program = r'''import sys
sys.path.insert(0, sys.argv[1])
from review_storage import write_json, read_json
payload = {'text': '\u041f\u0440\u0438\u0432\u0435\u0442'}
write_json(sys.argv[2], payload)
assert read_json(sys.argv[2]) == payload
print('OK')
'''
        env = {**os.environ, 'LC_ALL': 'C', 'LANG': 'C', 'PYTHONUTF8': '0', 'PYTHONCOERCECLOCALE': '0'}
        result = subprocess.run([sys.executable, '-c', program, str(Path(review_storage.__file__).parent),
                                 str(self.root / 'utf8.json')], env=env, capture_output=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr.decode('ascii', errors='replace'))
        self.assertIn('Привет', (self.root / 'utf8.json').read_text(encoding='utf-8'))

    def test_atomic_replacement_syncs_file_before_directory(self):
        events = []
        fsync, replace = os.fsync, os.replace

        def sync(fd):
            events.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
            fsync(fd)

        def rename(before, after):
            events.append('replace')
            replace(before, after)

        with patch.object(review_storage.os, 'fsync', side_effect=sync), \
                patch.object(review_storage.os, 'replace', side_effect=rename):
            review_storage.write_json(self.root / 'durable.json', {'ready': True})
        self.assertEqual(['file', 'replace', 'directory'], events)

    def test_workflow_names_can_contain_slashes(self):
        self.assertEqual({('CI/Backend', 'tests')}, review_ci.expected_checks([('CI/Backend', 'tests')]))


if __name__ == '__main__':
    unittest.main()
