"""Regression checks for complete, read-only daily log collection."""

import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import service_log_review as review


class ReviewTests(unittest.TestCase):
    def test_inventory_includes_package_provided_service_units(self):
        def command(_collector, *args):
            if review.SYSTEM + 'nix' in args:
                return '["custom"]'
            if 'list-unit-files' in args and '--user' not in args:
                return 'cockpit.service static -\n'
            if 'show' in args:
                return '\n\n'.join('Id=' + name + '\nActiveState=inactive'
                                    for name in args if name.endswith('.service'))
            return ''

        previous_mask = os.umask(0o077)
        self.addCleanup(os.umask, previous_mask)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(review, 'ROOT', Path(directory)), \
                patch.object(review.Collector, 'command', command), \
                patch.object(review.Collector, 'scan_journal'), patch('builtins.print'):
            result = review.main()
        self.assertEqual({row['source'] for row in result['configured_units']},
                         {'system:custom.service', 'system:cockpit.service'})

    def test_overlapping_runs_keep_matched_independent_evidence(self):
        barrier = threading.Barrier(2)
        previous_mask = os.umask(0o077)
        self.addCleanup(os.umask, previous_mask)

        def command(_collector, *args):
            if review.SYSTEM + 'nix' in args:
                return '["sample"]'
            if 'show' in args:
                return 'Id=sample.service\nActiveState=active\nResult=success\nExecMainStatus=0\n'
            return ''

        def journal(collector, _args):
            barrier.wait(timeout=10)
            collector.record('system:sample.service', 'error: fixture')

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(review, 'ROOT', Path(directory)), \
                patch.object(review.Collector, 'command', command), \
                patch.object(review.Collector, 'scan_journal', journal), \
                patch('builtins.print'):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: review.main(), range(2)))
            self.assertNotEqual(results[0]['events_file'], results[1]['events_file'])
            for result in results:
                events = Path(result['events_file'])
                self.assertEqual(len(events.read_text().splitlines()), 1)
                stored = json.loads((events.parent / 'summary.json').read_text())
                self.assertEqual(stored['log_counts']['system:sample.service']['flagged'], 1)

    def test_application_warnings_and_structured_errors_at_info_priority(self):
        for message in ('[WRN] query slow', '[Error] RPC failed',
                        '{"level":2,"message":"preview missing"}'):
            self.assertTrue(review.flagged(message), message)
        self.assertTrue(review.flagged('disk almost full', priority='4'))
        self.assertFalse(review.flagged('{"level":1,"message":"error count is zero"}'))

    def test_command_echoes_and_sudo_audit_are_not_application_errors(self):
        self.assertFalse(review.flagged('$ curl --fail --show-error http://localhost'))
        self.assertFalse(review.flagged('COMMAND=journalctl --grep=error', identifier='sudo'))
        self.assertTrue(review.flagged('authentication failure', priority=3, identifier='sudo'))

    def test_credentials_are_redacted_before_recording(self):
        message = ('password="two words" api_key=abc secret=xyz token=abc '
                   'Authorization: Bearer abc.def https://user:pw@example.com/?token=xyz')
        value = review.redact(message)
        for secret in ('two words', 'abc', '.def', 'xyz', 'user:pw'):
            self.assertNotIn(secret, value)

    def test_busy_logs_retain_all_flagged_events_without_truncation(self):
        output = io.StringIO()
        collector = review.Collector(output)
        collector.record('system:quiet.service', 'completed')
        for number in range(1001):
            collector.record('system:busy.service', f'error {number} ' + 'x' * 5000)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(events), 1001)
        self.assertGreater(len(events[-1]['message']), 5000)
        self.assertEqual(collector.counts['system:busy.service']['flagged'], 1001)
        self.assertEqual(collector.counts['system:quiet.service']['entries'], 1)
        self.assertEqual(collector.counts['system:quiet.service']['flagged'], 0)

    def test_user_journal_ownership_and_inaccessible_sources_are_preserved(self):
        output = io.StringIO()
        collector = review.Collector(output)
        entry = {"_SYSTEMD_UNIT": "user@1000.service", "_SYSTEMD_USER_UNIT": "token-eye-update.service",
                 "_UID": "1000", "MESSAGE": "error: refresh failed", "PRIORITY": "6"}
        collector.scan_journal([sys.executable, '-c', 'print(' + repr(json.dumps(entry)) + ')'])
        self.assertEqual(json.loads(output.getvalue())['source'], 'user:1000:token-eye-update.service')
        collector.scan_journal([sys.executable, '-c',
                                'import sys; sys.stderr.write("access denied"); sys.exit(1)'])
        self.assertEqual(collector.gaps, [{"source": "journal", "error": "access denied"}])

    def test_success_result_does_not_hide_nonzero_last_execution(self):
        rows = review.properties('Id=update.service\nResult=success\nExecMainStatus=1\n\n'
                                 'Id=idle.service\nActiveState=inactive\n')
        self.assertEqual(rows[0]['ExecMainStatus'], '1')
        self.assertEqual(rows[1]['ActiveState'], 'inactive')


if __name__ == '__main__':
    unittest.main()
