"""Isolated systemd unit/lifecycle tests; no real systemd installation is performed.

Only temporary files are modified. systemctl, user switching, account lookup and
privileged ownership changes are simulated explicitly at the OS boundary.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
from io import StringIO
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "discuz_archive.py"
SPEC = importlib.util.spec_from_file_location("discuz_archive_systemd_tests", SCRIPT)
archive = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = archive
SPEC.loader.exec_module(archive)


class UnitBuildTests(unittest.TestCase):
    def units(self, web=True, user="archive", calendar="*-*-* 03:00:00"):
        return archive.build_units(
            Path('/opt/archive space/100%$config.toml'),
            Path('/opt/archive space/main.py'),
            '/usr/bin/python3', user, calendar, web,
        )

    def test_oneshot_and_persistent_daily_timer(self):
        units = self.units()
        self.assertEqual(set(units), set(archive.UNIT_NAMES))
        crawler = units['discuz-archive.service']
        self.assertTrue(crawler.startswith(archive.UNIT_MARKER + '\n'))
        self.assertIn('Type=oneshot\n', crawler)
        self.assertNotIn('RemainAfterExit', crawler)
        self.assertIn('User=archive\n', crawler)
        self.assertIn('NoNewPrivileges=true\n', crawler)
        self.assertIn('UMask=0077\n', crawler)
        timer = units['discuz-archive.timer']
        self.assertIn('OnCalendar=*-*-* 03:00:00\n', timer)
        self.assertIn('Persistent=true\n', timer)
        self.assertIn('Unit=discuz-archive.service\n', timer)
        self.assertIn('"--serve"', units['discuz-archive-web.service'])

    def test_unit_arguments_preserve_spaces_percent_and_dollar(self):
        crawler = self.units()['discuz-archive.service']
        self.assertIn('ExecStart=:"/usr/bin/python3"', crawler)
        self.assertIn('"/opt/archive space/main.py"', crawler)
        self.assertIn('"--config" "/opt/archive space/100%%$config.toml"', crawler)
        self.assertEqual(archive.unit_quote('a"b\\c%$'), '"a\\"b\\\\c%%$"')

    def test_reader_is_optional(self):
        self.assertEqual(set(self.units(web=False)), {
            'discuz-archive.service', 'discuz-archive.timer',
        })

    def test_rejects_root_and_configuration_injection(self):
        for user in ('root', 'root\nUser=archive', 'x\x00y', 'user;command'):
            with self.subTest(user=user), self.assertRaises(ValueError):
                self.units(user=user)
        for calendar in ('', 'daily\nUnit=evil.service', 'daily\rUnit=x', '%H', 'daily\\'):
            with self.subTest(calendar=calendar), self.assertRaises(ValueError):
                self.units(calendar=calendar)
        for value in ('a\nb', 'a\rb', 'a\x00b', 'a\x7fb'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                archive.unit_quote(value)
        with self.assertRaises(ValueError):
            archive.build_units(Path('/opt/c'), Path('/opt/a'), 'python3', 'archive', 'daily', False)

    def test_owned_units_only_and_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            self.assertEqual(archive._managed_units(directory), {})
            unit = directory / 'discuz-archive.service'
            unit.write_text(archive.UNIT_MARKER + '\n[Service]\n', encoding='utf-8')
            self.assertEqual(archive._managed_units(directory), {unit.name: unit})
            foreign = directory / 'discuz-archive.timer'
            foreign.write_text('# managed by someone else\n', encoding='utf-8')
            with self.assertRaises(RuntimeError):
                archive._managed_units(directory)
            foreign.unlink()
            foreign.symlink_to(unit)
            with self.assertRaises(RuntimeError):
                archive._managed_units(directory)


class InstallerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.units = self.root / 'units'
        self.units.mkdir()
        self.config_path = self.root / 'config.toml'
        self.config_path.write_text('test configuration\n', encoding='utf-8')
        self.config = SimpleNamespace(
            output_dir=self.root / 'output', db_path=self.root / 'data/archive.sqlite3',
            schedule='*-*-* 03:00:00', web_enabled=True,
            cookie_file=None, web_token_file=None,
        )
        self.config.output_dir.mkdir()
        self.config.db_path.parent.mkdir()
        self.config.db_path.write_bytes(b'preserved history fixture')
        self.saved = self.config.output_dir / 'Original title.html'
        self.saved.write_text('first archived version', encoding='utf-8')
        self.calls = []
        self.output = StringIO()
        self.addCleanup(patch.stopall)
        patch.object(archive, 'UNIT_DIRECTORY', self.units).start()
        patch.object(archive.sys, 'platform', 'linux').start()
        patch.object(archive.os, 'geteuid', return_value=0).start()
        real_is_dir = Path.is_dir
        patch.object(Path, 'is_dir', lambda path: True if str(path) == '/run/systemd/system'
                     else real_is_dir(path)).start()
        self.access = patch.object(archive, '_ensure_service_access').start()
        self.systemctl = patch.object(archive, '_systemctl', side_effect=self.record).start()
        patch.object(archive.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name).start()
        self.run = patch.object(archive.subprocess, 'run', return_value=
                                subprocess.CompletedProcess([], 0, '', '')).start()
        patch.dict(os.environ, {'SUDO_USER': 'archive'}, clear=False).start()

    def record(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, '', '')

    def install(self, **kwargs):
        with redirect_stdout(self.output):
            archive.install_service(self.config, self.config_path, **kwargs)

    def test_install_reinstall_and_changed_calendar(self):
        self.install()
        self.assertEqual({p.name for p in self.units.iterdir()}, set(archive.UNIT_NAMES))
        self.assertIn((('enable', '--now', 'discuz-archive.timer'), {}), self.calls)
        self.assertIn((('enable', '--now', 'discuz-archive-web.service'), {}), self.calls)
        self.access.assert_called_once()
        self.assertTrue(any(call.args[0][1] == 'verify' for call in self.run.call_args_list))
        self.config.schedule = '*-*-* 04:30:00'
        self.install()
        self.assertIn('OnCalendar=*-*-* 04:30:00',
                      (self.units / 'discuz-archive.timer').read_text())
        self.assertEqual(self.saved.read_text(), 'first archived version')
        self.assertEqual(self.config.db_path.read_bytes(), b'preserved history fixture')
        self.assertIn((('stop', 'discuz-archive.service'), {}), self.calls)

    def test_disable_reader_during_reinstall(self):
        self.install()
        self.config.web_enabled = False
        self.install()
        self.assertFalse((self.units / 'discuz-archive-web.service').exists())
        self.assertTrue((self.units / 'discuz-archive.timer').exists())
        self.assertIn((('disable', 'discuz-archive-web.service'), {}), self.calls)

    def test_uninstall_is_idempotent_and_preserves_user_files(self):
        self.install()
        foreign = self.units / 'unrelated.service'
        foreign.write_text('unrelated unit', encoding='utf-8')
        self.calls.clear()
        self.install(uninstall=True)
        self.assertEqual(list(self.units.iterdir()), [foreign])
        self.assertIn((('disable', '--now', 'discuz-archive.timer'), {}), self.calls)
        self.assertIn((('stop', 'discuz-archive.service'), {}), self.calls)
        self.assertIn((('disable', '--now', 'discuz-archive-web.service'), {}), self.calls)
        self.assertEqual(self.saved.read_text(), 'first archived version')
        self.assertEqual(self.config.db_path.read_bytes(), b'preserved history fixture')
        self.assertEqual(self.config_path.read_text(), 'test configuration\n')
        self.calls.clear()
        self.install(uninstall=True)
        self.assertEqual(self.calls, [])

    def test_foreign_unit_collision_aborts_before_any_changes(self):
        for uninstall in (False, True):
            with self.subTest(uninstall=uninstall):
                foreign = self.units / 'discuz-archive.timer'
                foreign.write_text('# owned by another program\n', encoding='utf-8')
                with self.assertRaises(RuntimeError):
                    self.install(uninstall=uninstall)
                self.assertEqual(self.calls, [])
                self.access.assert_not_called()
                self.assertEqual(foreign.read_text(), '# owned by another program\n')

    def test_symlink_collision_aborts(self):
        foreign = self.units / 'discuz-archive.service'
        foreign.symlink_to(self.config_path)
        with self.assertRaises(RuntimeError):
            self.install(uninstall=True)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.config_path.read_text(), 'test configuration\n')

    def test_dry_run_has_no_files_accounts_or_subprocess_side_effects(self):
        for uninstall in (False, True):
            with self.subTest(uninstall=uninstall), patch.object(archive.sys, 'platform', 'darwin'):
                self.install(uninstall=uninstall, dry_run=True)
        self.assertEqual(list(self.units.iterdir()), [])
        self.access.assert_not_called()
        self.systemctl.assert_not_called()
        self.run.assert_not_called()
        self.assertIn('discuz-archive.timer', self.output.getvalue())

    def test_root_required_only_for_actual_mutation(self):
        with patch.object(archive.os, 'geteuid', return_value=1000), self.assertRaises(RuntimeError):
            self.install()
        self.assertEqual(list(self.units.iterdir()), [])
        self.systemctl.assert_not_called()
        self.access.assert_not_called()

    def test_platform_check_precedes_mutation(self):
        with patch.object(archive.sys, 'platform', 'darwin'), self.assertRaises(RuntimeError):
            self.install()
        self.assertEqual(list(self.units.iterdir()), [])
        self.access.assert_not_called()
        self.systemctl.assert_not_called()

    def test_failed_stop_preserves_existing_unit_files(self):
        self.install()
        originals = {p.name: p.read_bytes() for p in self.units.iterdir()}
        self.systemctl.side_effect = RuntimeError('simulated systemctl failure')
        with self.assertRaises(RuntimeError):
            self.install(uninstall=True)
        self.assertEqual({p.name: p.read_bytes() for p in self.units.iterdir()}, originals)


class ServiceAccessTests(unittest.TestCase):
    def test_named_root_files_and_token_are_checked_without_recursive_takeover(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            output = root / 'output'
            output.mkdir()
            state = root / 'data'
            state.mkdir()
            db = state / 'history.sqlite3'
            config_path = root / 'config.toml'
            token = root / 'token.txt'
            cookie = root / 'cookies.txt'
            for file in (db, config_path, token, cookie, Path(str(db) + '.lock'),
                         output / '.discuz-archive.json', output / 'Original.html',
                         output / '.pending-123.tmp'):
                file.write_text('fixture', encoding='utf-8')
            unrelated = output / 'unrelated.bin'
            unrelated.write_bytes(b'not owned by this tool')
            other_owned = output / 'Other user.html'
            other_owned.write_text('other owner fixture', encoding='utf-8')
            config = SimpleNamespace(output_dir=output, db_path=db, cookie_file=cookie,
                                     web_token_file=token)
            account = SimpleNamespace(pw_uid=1234, pw_gid=1234)
            real_stat = Path.stat

            def ownership_stat(path, *args, **kwargs):
                result = real_stat(path, *args, **kwargs)
                fields = list(result)
                fields[4] = 5555 if path == other_owned else 0
                return os.stat_result(fields)

            with patch.object(archive.pwd, 'getpwnam', return_value=account), \
                 patch.object(archive.os, 'chown') as chown, \
                 patch.object(archive.shutil, 'which', return_value='/usr/sbin/runuser'), \
                 patch.object(archive.subprocess, 'run', return_value=
                              subprocess.CompletedProcess([], 0, b'', b'')) as run, \
                 patch.object(Path, 'stat', ownership_stat), \
                 patch.object(Path, 'parents', property(lambda self: ())):
                # Only this isolated permission-check test treats its temporary
                # paths as installed paths; production rejects PrivateTmp paths.
                archive._ensure_service_access(config, config_path, SCRIPT, sys.executable, 'archive')
            command = run.call_args.args[0]
            self.assertIn(str(token.resolve()), command)
            self.assertIn(str(cookie.resolve()), command)
            self.assertIn(str(other_owned), command)
            changed = [call.args[0] for call in chown.call_args_list]
            self.assertIn(Path(str(db) + '.lock'), changed)
            self.assertIn(output / '.discuz-archive.json', changed)
            self.assertIn(output / '.pending-123.tmp', changed)
            self.assertNotIn(unrelated, changed)
            self.assertNotIn(other_owned, changed)
            self.assertTrue(all(call.kwargs.get('follow_symlinks') is False
                                for call in chown.call_args_list))

    def test_uid_zero_account_rejected(self):
        config = SimpleNamespace(output_dir=Path('/opt/a/output'), db_path=Path('/opt/a/data/db'))
        with patch.object(archive.pwd, 'getpwnam', return_value=
                          SimpleNamespace(pw_uid=0, pw_gid=0)), self.assertRaises(RuntimeError):
            archive._ensure_service_access(config, Path('/opt/a/c'), SCRIPT, sys.executable, 'archive')


if __name__ == '__main__':
    unittest.main()
