"""Read-only service access diagnostics under the actual test process account.

No root, runuser, chmod of project files, or system service mutation is involved.
Temporary fixtures contain a sentinel to prove diagnostics never read secrets.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import discuz_archive as archive


class ServiceDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='discuz-permission-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root/'config.toml'
        self.script = self.root/'archiver.py'
        self.secret = self.root/'cookie.txt'
        self.sentinel = 'NEUTRAL_SECRET_CONTENT_MUST_NOT_APPEAR'
        for path in (self.config, self.script, self.secret):
            path.write_text(self.sentinel)
        self.output = self.root/'output'
        self.output.mkdir()
        self.state = self.root/'state.sqlite3'
        self.state.write_bytes(b'neutral fixture')
        self.readable = [self.config, self.script, Path(sys.executable), self.secret]

    def probe(self, *, readable=None, directories=None, files=None):
        readable = self.readable if readable is None else readable
        directories = [self.output] if directories is None else directories
        files = [self.state] if files is None else files
        result = subprocess.run(
            [sys.executable, '-B', '-c', archive.service_access_code(),
             str(len(readable)), str(len(directories)),
             *(str(p) for p in [*readable, *directories, *files])],
            capture_output=True, text=True, timeout=10,
        )
        self.assertNotIn(self.sentinel, result.stdout+result.stderr)
        return result, json.loads(result.stdout)

    def assert_failure(self, report, operation, path):
        self.assertTrue(any(item['operation'] == operation and item['path'] == str(path)
                            for item in report['failures']), report)

    def test_accessible_paths_pass_with_actual_uid_and_without_reading_contents(self):
        result, report = self.probe()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report['uid'], os.geteuid())
        self.assertEqual(report['failures'], [])

    def test_missing_readable_file_names_the_exact_path(self):
        missing = self.root/'missing-cookie.txt'
        result, report = self.probe(readable=self.readable+[missing])
        self.assertEqual(result.returncode, 3)
        self.assert_failure(report, 'missing', missing)
        error = archive.format_service_access_error('archive', result.stdout, sys.executable)
        self.assertIn("'archive'", error)
        self.assertIn(str(missing), error)
        self.assertNotIn(self.sentinel, error)

    @unittest.skipIf(os.geteuid() == 0, 'Root bypasses ordinary read permission bits')
    def test_unreadable_cookie_reports_read_permission_not_its_contents(self):
        self.secret.chmod(0)
        try:
            result, report = self.probe()
            self.assertEqual(result.returncode, 3)
            self.assert_failure(report, 'read', self.secret)
            self.assertNotIn(self.sentinel, archive.format_service_access_error('archive', result.stdout, sys.executable))
        finally:
            self.secret.chmod(0o600)

    @unittest.skipIf(os.geteuid() == 0, 'Root can traverse mode-zero directories')
    def test_first_blocking_parent_is_named_for_nested_file(self):
        blocked = self.root/'private-parent'
        blocked.mkdir()
        child = blocked/'nested'
        child.mkdir()
        token = child/'token.txt'
        token.write_text(self.sentinel)
        blocked.chmod(0)
        try:
            result, report = self.probe(readable=self.readable+[token])
            self.assertEqual(result.returncode, 3)
            self.assert_failure(report, 'traverse', blocked)
            failures = [item for item in report['failures'] if item['target'] == str(token)]
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]['path'], str(blocked))
            error = archive.format_service_access_error('archive', result.stdout, sys.executable)
            self.assertIn(str(blocked), error)
            self.assertIn(str(token), error)
        finally:
            blocked.chmod(0o700)

    @unittest.skipIf(os.geteuid() == 0, 'Root bypasses ordinary write permission bits')
    def test_directory_and_existing_state_write_failures_are_distinct(self):
        self.output.chmod(0o500)
        self.state.chmod(0o400)
        try:
            result, report = self.probe()
            self.assertEqual(result.returncode, 3)
            self.assert_failure(report, 'write', self.output)
            self.assert_failure(report, 'write', self.state)
        finally:
            self.output.chmod(0o700)
            self.state.chmod(0o600)

    def test_configured_python_is_checked_for_execute_as_well_as_read(self):
        interpreter = self.root/'private-python'
        interpreter.write_text('neutral executable fixture')
        interpreter.chmod(0o600)
        readable = [self.config, self.script, interpreter]
        result, report = self.probe(readable=readable)
        self.assertEqual(result.returncode, 3)
        self.assert_failure(report, 'execute', interpreter)
        error = archive.format_service_access_error('archive', result.stdout, str(interpreter))
        self.assertIn('cannot execute interpreter', error)
        self.assertIn(str(interpreter), error)

    def test_working_directory_requires_traversal(self):
        workdir = self.root/'working'
        workdir.mkdir()
        config = workdir/'config.toml'
        config.write_text('neutral config fixture')
        workdir.chmod(0o600)
        try:
            if os.access(workdir, os.X_OK):
                self.skipTest('Current account bypasses directory traverse permissions')
            result, report = self.probe(readable=[config, self.script, Path(sys.executable)])
            self.assertEqual(result.returncode, 3)
            self.assert_failure(report, 'traverse', workdir)
        finally:
            workdir.chmod(0o700)

    def test_formatter_does_not_echo_unrecognized_fields_or_launcher_stderr(self):
        stdout = json.dumps(dict(failures=[dict(operation='read', path='/opt/archive/config.toml',
                                                 target='/opt/archive/config.toml', secret=self.sentinel)],
                                 secret=self.sentinel))
        error = archive.format_service_access_error('archive', stdout, '/usr/bin/python3')
        self.assertIn('/opt/archive/config.toml', error)
        self.assertNotIn(self.sentinel, error)
        for malformed in (None, self.sentinel, 'null', '[]', '{', '{"failures":[]}'):
            with self.subTest(stdout=malformed):
                result = archive.format_service_access_error('archive', malformed, '/root/private/python')
                self.assertIn('Python interpreter', result)
                self.assertIn('/root/private/python', result)
                self.assertIn('parent', result)
                self.assertNotIn(self.sentinel, result)

    def test_formatter_strips_control_characters_and_limits_diagnostic_lines(self):
        failures = [dict(operation='read', path='/opt/file'+str(i)+'\x1b\n',
                         target='/opt/file'+str(i)+'\x1b\n') for i in range(100)]
        error = archive.format_service_access_error('archive', json.dumps(dict(failures=failures)), '/usr/bin/python3')
        self.assertNotIn('\x1b', error)
        self.assertLessEqual(error.count('\n- '), 8)

    def run_fallback_check(self, report, fallback_code):
        directory = self.root/'data'
        directory.mkdir()
        config = SimpleNamespace(output_dir=self.output, db_path=directory/'archive.sqlite3',
                                 cookie_file=None, web_token_file=None)
        configured_python = '/root/.pyenv/shims/python3'
        target_user = 'discuz-archive'
        primary = subprocess.CompletedProcess([], 126, b'', self.sentinel.encode())
        fallback = subprocess.CompletedProcess([], fallback_code, json.dumps(report).encode(), b'')
        real_is_file = Path.is_file
        with patch.object(archive.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=12345, pw_gid=12345)), \
             patch.object(archive.os, 'chown'), \
             patch.object(archive.shutil, 'which', return_value='/usr/sbin/runuser'), \
             patch.object(Path, 'parents', property(lambda path: ())), \
             patch.object(Path, 'is_file', lambda path: True if str(path) == '/usr/bin/python3' else real_is_file(path)), \
             patch.object(archive.subprocess, 'run', side_effect=[primary, fallback]) as run:
            # Only the OS boundary is simulated; these temporary paths are
            # treated as persistent installation paths to avoid PrivateTmp.
            with self.assertRaises(RuntimeError) as caught:
                archive._ensure_service_access(config, self.config, self.script,
                                               configured_python, target_user)
        self.assertEqual(run.call_count, 2)
        first, second = [call.args[0] for call in run.call_args_list]
        self.assertEqual(first[:5], ['/usr/sbin/runuser', '-u', target_user, '--', configured_python])
        self.assertEqual(second[:5], ['/usr/sbin/runuser', '-u', target_user, '--', '/usr/bin/python3'])
        self.assertIn(configured_python, second[7:], 'Fallback must still test configured interpreter metadata')
        self.assertTrue(all(call.kwargs.get('cwd') == '/' for call in run.call_args_list))
        message = str(caught.exception)
        self.assertNotIn(self.sentinel, message)
        return message

    def test_public_python_fallback_names_blocked_parent_and_service_user(self):
        report = dict(uid=12345, failures=[dict(operation='traverse', path='/root',
                                              target='/root/.pyenv/shims/python3')], total_failures=1)
        message = self.run_fallback_check(report, 3)
        self.assertIn('discuz-archive', message)
        self.assertIn('cannot traverse directory: /root', message)
        self.assertIn('/root/.pyenv/shims/python3', message)

    def test_successful_metadata_fallback_does_not_accept_broken_target_python(self):
        message = self.run_fallback_check(dict(uid=12345, failures=[], total_failures=0), 0)
        self.assertIn('discuz-archive', message)
        self.assertIn('Python interpreter', message)
        self.assertIn('/root/.pyenv/shims/python3', message)
        self.assertIn('could not start', message)


if __name__ == '__main__':
    unittest.main()
