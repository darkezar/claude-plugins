"""Служебная запись не импортирует PR; ремонт карты не обходит общую блокировку."""

import fcntl
import json
import os
from pathlib import Path
import select
import shlex
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/review-kit/scripts'
sys.path.insert(0, str(SCRIPTS))
import reply_map
from review_storage import read_json, write_json


class RoundFourRegressionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.pr = 'https://github.com/owner/repo/pull/1'
        self.poison_marker = self.root / 'imported-from-pr'
        for module in ('json', 'tempfile'):
            (self.repo / (module + '.py')).write_text(
                f'open({str(self.poison_marker)!r}, "w").write("imported")\n'
                'raise RuntimeError("untrusted module imported")\n', encoding='utf-8')

    def test_storage_cli_writes_json_without_importing_pr_or_pythonpath(self):
        output = self.root / 'report.json'
        payload = {'verdict': 'READY AFTER FIXES', 'text': 'Данные, не Python: __import__("os")'}
        result = subprocess.run([sys.executable, '-I', str(SCRIPTS / 'review_storage.py'),
            'write', '--output', str(output)], input=json.dumps(payload).encode('utf-8'),
            cwd=self.repo, env={**os.environ, 'PYTHONPATH': str(self.repo)},
            capture_output=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(output.exists(), 'CLI must actually write the report')
        self.assertEqual(payload, read_json(output))
        self.assertEqual(0o600, output.stat().st_mode & 0o777)
        self.assertFalse(self.poison_marker.exists())

    def test_claude_journal_recipe_runs_from_untrusted_pr(self):
        skill = (SCRIPTS.parent / 'skills/agent-full-review/SKILL.md').read_text(encoding='utf-8')
        # Исполняется сам документированный сценарий, включая выбор cwd и пути журнала.
        recipe = next(block.split('```', 1)[0] for block in skill.split('```bash\n')[1:]
                      if 'review-log.jsonl' in block.split('```', 1)[0]
                      and 'REPORT_FILE' in block.split('```', 1)[0])
        report = self.root / 'report.json'
        payload = {'verdict': 'READY AFTER FIXES', 'findings': ['Проверка']}
        write_json(report, payload)
        cwd_probe = self.root / 'python-cwd'
        wrapper = self.root / 'python3'
        wrapper.write_text('#!/bin/sh\npwd > ' + shlex.quote(str(cwd_probe)) + '\nexec '
                           + shlex.quote(sys.executable) + ' "$@"\n', encoding='utf-8')
        wrapper.chmod(0o755)
        env = {**os.environ, 'REPORT_FILE': str(report), 'PYTHONPATH': str(self.repo),
               'PATH': str(self.root) + os.pathsep + os.environ['PATH']}
        env.pop('ARTIFACT_DIR', None)
        result = subprocess.run(['bash', '-c', recipe], cwd=self.repo,
            env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(self.root.resolve(), Path(cwd_probe.read_text().strip()).resolve())
        self.assertFalse(self.poison_marker.exists())
        lines = (self.repo / '.git/review-log.jsonl').read_text(encoding='utf-8').splitlines()
        self.assertEqual([payload], [json.loads(line) for line in lines])

    def prepare_legacy_map(self):
        old = {'version': 1, 'pr': self.pr, 'revision': 4, 'history': ['old report'],
               'items': [{'comment_id': 'C1', 'state': 'local', 'legacy_note': 'keep'}]}
        path = reply_map.map_path(self.repo, self.pr, create=True)
        write_json(path, old)
        candidate = self.root / 'candidate.json'
        write_json(candidate, {'version': 1, 'pr': self.pr, 'revision': 4,
            'items': [{'comment_id': 'C1', 'thread_id': 'T1', 'state': 'local',
                       'resolved': False, 'reply_verified': False}]})
        command = [sys.executable, reply_map.__file__, 'repair', '--repo', str(self.repo),
                   '--pr', self.pr, '--input', str(candidate)]
        return path, candidate, command

    def test_repair_preserves_original_backup_metadata_and_revision(self):
        path, candidate, command = self.prepare_legacy_map()
        original = path.read_bytes()
        repaired = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(0, repaired.returncode, repaired.stderr)
        receipt = json.loads(repaired.stdout)
        backup = Path(receipt['backup'])
        self.assertEqual(original, backup.read_bytes())
        self.assertEqual(0o600, backup.stat().st_mode & 0o777)
        self.assertEqual(str(path), receipt['path'])
        self.assertEqual(5, receipt['revision'])
        current = reply_map.load_map(self.repo, self.pr)
        self.assertEqual(['old report'], current['history'])
        self.assertEqual('keep', current['items'][0]['legacy_note'])
        self.assertEqual('T1', current['items'][0]['thread_id'])
        stale = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(3, stale.returncode, stale.stderr)
        self.assertEqual(current, reply_map.load_map(self.repo, self.pr))
        self.assertEqual(1, len(list(path.parent.glob('*backup*'))))

    def test_repair_obeys_the_same_lock_file_as_save(self):
        path, candidate, command = self.prepare_legacy_map()
        original = path.read_bytes()
        # Имя независимо задано контрактом обычного save: <sha>.lock, не .json.lock.
        descriptor = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, 'r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            program = '''import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).resolve().parent))
import reply_map
original = reply_map.fcntl.flock
def observed(fd, operation):
    info = os.fstat(fd)
    print(json.dumps({'lock_inode': info.st_ino, 'lock_device': info.st_dev}), flush=True)
    return original(fd, operation)
reply_map.fcntl.flock = observed
sys.argv = sys.argv[1:]
reply_map.main()
'''
            process = subprocess.Popen([sys.executable, '-I', '-c', program, *command[1:]],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                # Сигнал приходит после старта Python непосредственно перед реальным flock.
                self.assertTrue(select.select([process.stdout], [], [], 10)[0])
                ready = json.loads(process.stdout.readline())
                info = os.fstat(lock.fileno())
                self.assertEqual({'lock_inode': info.st_ino, 'lock_device': info.st_dev}, ready)
                self.assertEqual(original, path.read_bytes())
                fcntl.flock(lock, fcntl.LOCK_UN)
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(0, process.returncode, stderr)
                self.assertEqual(5, json.loads(stdout)['revision'])
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_repair_cannot_drop_old_comment_ids_or_bypass_storage_checks(self):
        path, candidate, command = self.prepare_legacy_map()
        original = path.read_bytes()
        for payload in ({'version': 1, 'pr': self.pr, 'revision': 4, 'items': []},
                        {'version': 1, 'pr': self.pr + '2', 'revision': 4, 'items': []}):
            write_json(candidate, payload)
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(1, result.returncode, result.stderr)
            self.assertEqual(original, path.read_bytes())
        write_json(candidate, {'version': 1, 'pr': self.pr, 'revision': 4,
            'items': [{'comment_id': 'C1', 'thread_id': 'T1', 'state': 'local',
                       'resolved': False, 'reply_verified': False}]})
        target = self.root / 'target.json'
        target.write_bytes(original)
        target.chmod(0o600)
        path.unlink()
        path.symlink_to(target)
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertEqual(original, target.read_bytes())

    def test_reply_map_argument_errors_have_exit_code_two(self):
        result = subprocess.run([sys.executable, reply_map.__file__, 'save', '--repo', str(self.repo),
            '--pr', self.pr], capture_output=True, text=True, timeout=10)
        self.assertEqual(2, result.returncode)


if __name__ == '__main__':
    unittest.main()
