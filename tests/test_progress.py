"""Live progress snapshots: real local files/processes, no external requests."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace

import discuz_archive as archive
from tests.test_discuz_archive import FixtureState, fixture_server


class ProgressSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='discuz-progress-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = archive.Config('neutral-progress', 'https://forum.example.org/index.php',
                                     self.root / 'output', self.root / 'data/archive.sqlite3')
        self.path = self.config.db_path.parent / 'progress.json'

    def test_missing_snapshot_is_none_without_creating_files(self):
        self.assertIsNone(archive.read_progress(self.config))
        self.assertFalse(self.path.exists())

    def test_snapshot_has_real_identity_timestamp_and_progress_fields(self):
        progress = archive.Progress(self.config)
        progress.update(status='running', phase='images', fid='19', tid='101',
                        image_attempts=3, image_available=2, image_failed=1)
        result = archive.read_progress(self.config)
        self.assertEqual(result['pid'], os.getpid())
        self.assertTrue(result['trace'])
        self.assertTrue(result['process_alive'])
        self.assertEqual(result['status'], 'running')
        self.assertEqual(result['phase'], 'images')
        self.assertEqual(result['fid'], '19')
        self.assertEqual(result['tid'], '101')
        self.assertEqual((result['image_attempts'], result['image_available'], result['image_failed']),
                         (3, 2, 1))
        stamp = datetime.fromisoformat(result['updated_at'].replace('Z', '+00:00'))
        self.assertEqual(stamp.utcoffset(), timezone.utc.utcoffset(stamp))
        self.assertLess(abs((datetime.now(timezone.utc) - stamp).total_seconds()), 10)

    def test_reader_in_an_independent_process_sees_the_snapshot(self):
        archive.Progress(self.config).update(status='running', phase='post', fid='19', tid='101')
        code = ('import json,sys; from pathlib import Path; import discuz_archive as a; '
                'r=Path(sys.argv[1]); c=a.Config("neutral-progress",'
                '"https://forum.example.org/index.php",r/"output",r/"data/archive.sqlite3"); '
                'print(json.dumps(a.read_progress(c)))')
        child = subprocess.run([sys.executable, '-B', '-c', code, str(self.root)],
                               cwd=Path(archive.__file__).resolve().parent,
                               capture_output=True, text=True, timeout=10, check=True)
        result = json.loads(child.stdout)
        self.assertEqual(result['pid'], os.getpid())
        self.assertEqual(result['tid'], '101')
        self.assertTrue(result['process_alive'])

    def test_dead_running_process_is_reported_interrupted_without_rewriting_disk(self):
        progress = archive.Progress(self.config)
        progress.update(status='running', phase='images')
        child = subprocess.run([sys.executable, '-c', 'import os; print(os.getpid())'],
                               capture_output=True, text=True, timeout=10, check=True)
        raw = json.loads(self.path.read_text())
        raw['pid'] = int(child.stdout.strip())
        self.path.write_text(json.dumps(raw))
        before = self.path.read_bytes()
        result = archive.read_progress(self.config)
        self.assertFalse(result['process_alive'])
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(before, self.path.read_bytes(), 'Diagnostic read must remain read-only')

    def test_finished_and_failed_statuses_are_not_changed_to_interrupted(self):
        progress = archive.Progress(self.config)
        for status in ('finished', 'failed'):
            with self.subTest(status=status):
                progress.update(status=status, phase='done')
                result = archive.read_progress(self.config)
                self.assertEqual(result['status'], status)

    def test_unknown_sensitive_fields_are_not_persisted(self):
        secret = 'NEUTRAL_SECRET_SENTINEL_DO_NOT_PERSIST'
        archive.Progress(self.config).update(status='running', phase='post', tid='101',
                                            url='https://example.invalid/?token='+secret,
                                            cookie=secret, authorization=secret)
        text = self.path.read_text()
        self.assertNotIn(secret, text)
        result = json.loads(text)
        for key in ('url', 'cookie', 'authorization'):
            self.assertNotIn(key, result)

    def test_partial_or_invalid_snapshot_does_not_break_diagnostics(self):
        self.path.parent.mkdir(parents=True)
        for data in ('{', 'null', '[]'):
            with self.subTest(data=data):
                self.path.write_text(data)
                self.assertIsNone(archive.read_progress(self.config))

    def test_updates_publish_complete_json_to_concurrent_readers(self):
        progress = archive.Progress(self.config)
        progress.update(status='running', phase='images', image_attempts=0)
        errors = []
        finished = threading.Event()

        def write():
            try:
                for number in range(50):
                    progress.update(image_attempts=number)
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        writer = threading.Thread(target=write)
        writer.start()
        try:
            while not finished.is_set():
                result = json.loads(self.path.read_text())
                self.assertIsInstance(result, dict)
                self.assertEqual(result['status'], 'running')
        finally:
            writer.join(timeout=10)
        self.assertFalse(writer.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(json.loads(self.path.read_text())['image_attempts'], 49)

    def test_real_http_crawl_emits_stages_and_publishes_finished_counters(self):
        state = FixtureState(pages={'19:1': ['101']})
        with fixture_server(state) as base:
            config = replace(self.config, url=base+'index.php', fids=['19'],
                             interval=0, timeout=3, retries=0)
            with self.assertLogs(archive.LOG, level='INFO') as captured:
                stats = archive.crawl(config)
        text = '\n'.join(captured.output)
        for event in ('crawl_started', 'home_started', 'boards_discovered',
                      'board_page_started', 'board_page_parsed', 'post_started',
                      'image_started', 'image_progress', 'archived', 'crawl_finished'):
            self.assertIn(event, text)
        self.assertNotIn(base, text, 'Default logs must not expose full source URLs')
        result = archive.read_progress(config)
        self.assertEqual(result['status'], 'finished')
        self.assertEqual(result['stats']['archived'], 1)
        self.assertEqual(result['stats']['failed'], 0)
        self.assertEqual(result['image_available'], 2)
        self.assertEqual(stats['archived'], 1)


if __name__ == '__main__':
    unittest.main()
