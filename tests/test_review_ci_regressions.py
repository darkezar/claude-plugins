"""Регрессии реальных ответов gh, движения базы и ограниченного ожидания."""

from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from test_review_ci import ci, job


class ReviewCIRegressionTest(unittest.TestCase):
    def test_real_gh_empty_checks_is_a_known_empty_list(self):
        for message in ("no checks reported on the 'feature' branch",
                        "no required checks reported on the 'feature' branch"):
            with self.subTest(message=message), patch.object(ci.subprocess, 'run', return_value=
                    subprocess.CompletedProcess([], 1, stdout='', stderr=message + '\n')):
                self.assertEqual([], ci.query(['pr', 'checks'], float('inf')))

    def test_missing_expected_check_waits_even_without_branch_protection(self):
        meta = {'headRefOid': 'head', 'baseRefOid': 'base'}
        with patch.object(ci, 'query', side_effect=[meta, [], [], meta]):
            result = ci.observation('owner/repo', 1, 'head', 'base', ('CI/tests',), float('inf'))
        self.assertEqual('pending', result['state'])
        self.assertEqual(['CI/tests'], result['missing'])

    def test_moved_base_does_not_discard_head_checks(self):
        before = {'headRefOid': 'head', 'baseRefOid': 'new-base'}
        after = {'headRefOid': 'head', 'baseRefOid': 'newer-base'}
        with patch.object(ci, 'query', side_effect=[before, [job()], [job()], after]):
            result = ci.observation('owner/repo', 1, 'head', 'old-base', (), float('inf'))
        self.assertEqual('pass', result['state'])
        self.assertTrue(result['base_moved'])
        self.assertEqual('newer-base', result['observed_base_sha'])

    def test_expected_job_cannot_be_impersonated_by_another_workflow(self):
        meta = {'headRefOid': 'head', 'baseRefOid': 'base'}
        fake = {**job(), 'workflow': 'Untrusted'}
        with patch.object(ci, 'query', side_effect=[meta, [fake], [], meta]):
            result = ci.observation('owner/repo', 1, 'head', 'base', ('CI/tests',), float('inf'))
        self.assertEqual('pending', result['state'])
        with patch.object(ci, 'query', side_effect=[meta, [job()], [], meta]):
            result = ci.observation('owner/repo', 1, 'head', 'base', ('CI/tests',), float('inf'))
        self.assertEqual('pass', result['state'])

    def test_wait_timeout_keeps_previously_observed_pending(self):
        pending = {'state': 'pending', 'reason': 'waiting', 'checks': [job(bucket='pending')], 'required': []}
        with patch.object(ci, 'observation', side_effect=[pending, subprocess.TimeoutExpired('gh', 2)]), \
                patch.object(ci.time, 'sleep'):
            result = ci.collect('owner/repo', 1, 'head', 'base', wait_seconds=60)
        self.assertEqual('pending', result['state'])
        self.assertIn('бюджет', result['reason'])

    def test_wait_accounts_for_poll_duration_before_sleeping(self):
        now = [0.0]
        def observe(*args):
            now[0] += 8
            return {'state': 'pending', 'reason': 'waiting', 'checks': [], 'required': []}
        with patch.object(ci, 'observation', side_effect=observe) as read, \
                patch.object(ci.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(ci.time, 'sleep') as sleep:
            result = ci.collect('owner/repo', 1, 'head', 'base', wait_seconds=10, interval=1)
        self.assertEqual('pending', result['state'])
        self.assertEqual(1, read.call_count)
        sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
