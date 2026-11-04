"""Image-threshold regression tests using neutral local HTTP and real SQLite.

No production forum, credentials, or archive data are accessed. The v1 schema
fixture below represents the previous shipped database contract explicitly.
"""
from __future__ import annotations

import base64
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

import discuz_archive as archive
from tests import test_discuz_archive as fixture


@dataclass
class ThresholdState(fixture.FixtureState):
    missing_gif: bool = False
    duplicate_png: bool = False
    permission_notice: bool = False
    failed_image_first: bool = False
    malformed_gif_url: bool = False
    extra_placeholder: bool = False
    duplicate_failed_gif: bool = False
    thread_status: int = 200

    def thread(self, tid: str) -> str:
        result = super().thread(tid)
        if self.failed_image_first:
            first = '<img src="media/photo.png" alt="原始静态图片" />'
            second = '<img src="static/image/common/none.gif" file="media/motion.gif" alt="原始动图" />'
            result = result.replace(first + second, second + first)
        if self.missing_gif:
            result = result.replace('file="media/motion.gif"', 'file="missing.gif"')
        if self.malformed_gif_url:
            result = result.replace('file="media/motion.gif"', 'file="http://[broken"')
        if self.extra_placeholder:
            result = result.replace(
                '<img src="media/photo.png" alt="原始静态图片" />',
                '<img src="media/photo.png" alt="原始静态图片" />'
                '<img src="static/image/common/none.gif" alt="懒加载占位图" />',
            )
        if self.duplicate_failed_gif:
            tag = '<img src="static/image/common/none.gif" file="media/motion.gif" alt="原始动图" />'
            result = result.replace(tag, tag + tag.replace('media/motion.gif', 'media/motion.gif#duplicate'), 1)
        if self.duplicate_png:
            result = result.replace(
                '<img src="media/photo.png" alt="原始静态图片" />',
                '<img src="media/photo.png" alt="原始静态图片" />'
                '<img src="media/photo.png" alt="重复静态图片" />',
            )
        if self.permission_notice:
            marker = '<div class="sign">' if self.modern else '<div class="signatures">'
            result = result.replace(
                marker,
                '<div class="attach_nopermission attach_tips">'
                '您需要登录才可以下载或查看附件<img src="media/private.gif"></div>' + marker,
                1,
            )
        return result


@contextmanager
def status_server(state: ThresholdState):
    """A small actual HTTP fixture for post-level access/error status codes."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            path = parsed.path.removeprefix(state.prefix)
            status = 200
            media_type = 'text/html; charset=utf-8'
            if path in {'index.php', ''}:
                state.counts['home'] += 1
                payload = state.home().encode()
            elif path == 'forumdisplay.php':
                payload = state.board(query.get('fid', ['19'])[0], 1).encode()
            elif path == 'viewthread.php':
                tid = query.get('tid', [''])[0]
                state.counts['thread:' + tid] += 1
                status = state.thread_status
                payload = state.thread(tid).encode()
            elif path == 'robots.txt':
                payload, media_type = b'User-agent: *\nAllow: /\n', 'text/plain'
            else:
                status, payload = 404, b'not found'
            self.send_response(status)
            self.send_header('Content-Type', media_type)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}{state.prefix}'
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


class ThresholdHTTPTests(unittest.TestCase):
    setUp = fixture.CrawlHTTPTests.setUp
    cli = fixture.CrawlHTTPTests.cli
    records = fixture.CrawlHTTPTests.records

    def write_config(self, base, minimum=2):
        path = fixture.CrawlHTTPTests.write_config(self, base, fids=[19])
        content = path.read_text()
        content = content.replace('[archive]\n', f'[archive]\nmin_images = {minimum}\n')
        path.write_text(content)
        return path

    def rows(self, table):
        self.assertIn(table, {'rejected', 'pending'})
        with closing(sqlite3.connect(self.root / 'data/archive.sqlite3')) as database:
            database.row_factory = sqlite3.Row
            return database.execute(f'SELECT * FROM {table} ORDER BY tid').fetchall()

    def assert_no_content_files(self):
        output = self.root / 'output'
        self.assertEqual(list(output.glob('*.html')), [])
        self.assertEqual(list(output.glob('.pending-*.tmp')), [])
        self.assertEqual(self.rows('pending'), [])

    def test_below_threshold_rejects_forever_even_after_lowering_threshold(self):
        state = ThresholdState(fail_image=True, pages={'19:1': ['101']})
        with fixture.fixture_server(state) as base:
            path = self.write_config(base, 2)
            self.cli(path)
            self.assertEqual(self.records(), [])
            rejected = self.rows('rejected')
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]['tid'], '101')
            self.assertEqual(rejected[0]['available_images'], 1)
            self.assertEqual(rejected[0]['min_images'], 2)
            self.assert_no_content_files()
            before = state.counts.copy()
            state.edited, state.fail_image = True, False
            self.cli(self.write_config(base, 1))
            self.cli(self.write_config(base, 0))
            for key in ('thread:101', 'path:media/photo.png', 'path:media/motion.gif'):
                self.assertEqual(state.counts[key], before[key], key)
            self.assertEqual(dict(self.rows('rejected')[0]), dict(rejected[0]))
            self.assertEqual(self.records(), [])
            self.assert_no_content_files()
            with closing(sqlite3.connect(self.root / 'data/archive.sqlite3')) as db:
                stats = json.loads(db.execute("SELECT value FROM meta WHERE key='last_run'").fetchone()[0])
            self.assertEqual(stats['skipped'], 1)
            self.assertEqual(stats['failed'], 0)

    def test_exact_threshold_saves_png_and_original_animated_gif(self):
        state = ThresholdState(pages={'19:1': ['101']})
        with fixture.fixture_server(state) as base:
            self.cli(self.write_config(base, 2))
        row = self.records()[0]
        html = (self.root / 'output' / row['filename']).read_text()
        self.assertIn(base64.b64encode(fixture.PNG).decode(), html)
        self.assertIn(base64.b64encode(fixture.GIF).decode(), html)
        self.assertEqual(self.rows('rejected'), [])

    def test_enough_pngs_archive_despite_unavailable_gif(self):
        state = ThresholdState(fail_image=True, pages={'19:1': ['101']})
        with fixture.fixture_server(state) as base:
            self.cli(self.write_config(base, 1))
        self.assertEqual(len(self.records()), 1)
        html = (self.root / 'output' / self.records()[0]['filename']).read_text()
        self.assertIn(base64.b64encode(fixture.PNG).decode(), html)
        self.assertNotIn('data:image/gif', html)
        self.assertNotIn('media/motion.gif', html)
        self.assertEqual(self.rows('rejected'), [])

    def test_failed_first_image_does_not_prevent_later_accessible_image(self):
        state = ThresholdState(failed_image_first=True, fail_image=True, pages={'19:1': ['101']})
        with fixture.fixture_server(state) as base:
            self.cli(self.write_config(base, 1))
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(state.counts['path:media/photo.png'], 1)
        self.assertEqual(self.rows('rejected'), [])

    def test_broken_image_link_does_not_count(self):
        state = ThresholdState(missing_gif=True, pages={'19:1': ['101']})
        with fixture.fixture_server(state) as base:
            self.cli(self.write_config(base, 2))
        self.assertEqual(self.rows('rejected')[0]['available_images'], 1)
        self.assertEqual(state.counts['path:missing.gif'], 1)
        self.assert_no_content_files()

    def test_malformed_image_url_is_unavailable_instead_of_aborting_crawl(self):
        state = ThresholdState(malformed_gif_url=True, pages={'19:1': ['101']})
        with fixture.fixture_server(state) as base:
            self.cli(self.write_config(base, 1))
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.rows('rejected'), [])
        html = (self.root / 'output' / self.records()[0]['filename']).read_text()
        self.assertIn(base64.b64encode(fixture.PNG).decode(), html)
        self.assertNotIn('http://[broken', html)

    def test_html_masquerading_as_image_does_not_count(self):
        state = ThresholdState(poison_image=True, pages={'19:1': ['101']})
        with fixture.fixture_server(state) as base:
            self.cli(self.write_config(base, 2))
        self.assertEqual(self.rows('rejected')[0]['available_images'], 1)
        self.assert_no_content_files()

    def test_duplicate_urls_and_gif_frames_do_not_inflate_image_count(self):
        state = ThresholdState(duplicate_png=True, pages={'19:1': ['101']})
        with fixture.fixture_server(state) as base:
            self.cli(self.write_config(base, 3))
        self.assertEqual(self.rows('rejected')[0]['available_images'], 2)
        self.assertEqual(state.counts['path:media/photo.png'], 1)
        self.assertEqual(state.counts['path:media/motion.gif'], 1)
        self.assert_no_content_files()

    def test_lazy_loading_placeholder_without_real_source_does_not_count(self):
        state = ThresholdState(extra_placeholder=True, pages={'19:1': ['101']})
        with fixture.fixture_server(state) as base:
            self.cli(self.write_config(base, 3))
        self.assertEqual(self.rows('rejected')[0]['available_images'], 2)
        self.assertEqual(state.counts['path:static/image/common/none.gif'], 0)
        self.assert_no_content_files()

    def test_duplicate_failed_url_with_fragment_is_requested_only_once(self):
        state = ThresholdState(duplicate_failed_gif=True, fail_image=True, pages={'19:1': ['101']})
        with fixture.fixture_server(state) as base:
            self.cli(self.write_config(base, 2))
        self.assertEqual(self.rows('rejected')[0]['available_images'], 1)
        self.assertEqual(state.counts['path:media/motion.gif'], 1)
        self.assert_no_content_files()

    def test_permission_notice_excludes_private_images_but_keeps_accessible_images(self):
        state = ThresholdState(modern=True, permission_notice=True, pages={'19:1': ['101']})
        with fixture.fixture_server(state) as base:
            self.cli(self.write_config(base, 2))
        self.assertEqual(len(self.records()), 1)
        html = (self.root / 'output' / self.records()[0]['filename']).read_text()
        self.assertNotIn('您需要登录', html)
        self.assertNotIn('private.gif', html)
        self.assertEqual(state.counts['path:media/private.gif'], 0)
        self.assertEqual(self.rows('rejected'), [])

    def test_login_page_is_zero_images_and_permanently_rejected(self):
        state = ThresholdState(login_only=True, pages={'19:1': ['101']})
        with fixture.fixture_server(state) as base:
            path = self.write_config(base, 1)
            self.cli(path)
            row = self.rows('rejected')[0]
            self.assertEqual(row['available_images'], 0)
            state.login_only = False
            self.cli(path)
            self.assertEqual(state.counts['thread:101'], 1)
        self.assert_no_content_files()

    def test_post_access_or_gone_http_status_is_permanent_zero(self):
        for status in (401, 403, 404, 410):
            with self.subTest(status=status), tempfile.TemporaryDirectory(prefix='threshold-status-') as temp:
                original = self.root
                self.root = Path(temp)
                try:
                    state = ThresholdState(thread_status=status, pages={'19:1': ['101']})
                    with status_server(state) as base:
                        path = self.write_config(base, 1)
                        self.cli(path)
                        self.cli(path)
                    self.assertEqual(state.counts['thread:101'], 1)
                    self.assertEqual(self.rows('rejected')[0]['available_images'], 0)
                    self.assert_no_content_files()
                finally:
                    self.root = original

    def test_post_server_error_is_not_a_false_permanent_rejection(self):
        state = ThresholdState(thread_status=503, pages={'19:1': ['101']})
        with status_server(state) as base:
            path = self.write_config(base, 1)
            self.cli(path, expected=1)
            self.cli(path, expected=1)
        self.assertEqual(state.counts['thread:101'], 2)
        self.assertEqual(self.rows('rejected'), [])
        self.assert_no_content_files()

    def test_unknown_template_is_not_a_false_permanent_rejection(self):
        class UnknownTemplate(ThresholdState):
            def thread(self, tid):
                return '<!doctype html><html><body><h1>Neutral custom template</h1><p>No main floor marker.</p></body></html>'

        state = UnknownTemplate(pages={'19:1': ['101']})
        with fixture.fixture_server(state) as base:
            path = self.write_config(base, 1)
            self.cli(path, expected=1)
            self.cli(path, expected=1)
        self.assertEqual(state.counts['thread:101'], 2)
        self.assertEqual(self.rows('rejected'), [])
        self.assert_no_content_files()

    def test_already_archived_posts_are_not_retroactively_rejected(self):
        state = ThresholdState(pages={'19:1': ['101']})
        with fixture.fixture_server(state) as base:
            self.cli(self.write_config(base, 2))
            first = dict(self.records()[0])
            file = self.root / 'output' / first['filename']
            content = file.read_bytes()
            before = state.counts.copy()
            self.cli(self.write_config(base, 100))
            self.assertEqual(state.counts['thread:101'], before['thread:101'])
        self.assertEqual(dict(self.records()[0]), first)
        self.assertEqual(file.read_bytes(), content)
        self.assertEqual(self.rows('rejected'), [])

    def test_zero_disables_threshold_and_preserves_full_image_retry_contract(self):
        state = ThresholdState(fail_image=True, pages={'19:1': ['101']})
        with fixture.fixture_server(state) as base:
            path = self.write_config(base, 0)
            self.cli(path, expected=1)
            self.assertEqual(self.rows('rejected'), [])
            state.fail_image = False
            self.cli(path)
            self.assertEqual(state.counts['thread:101'], 2)
        self.assertEqual(len(self.records()), 1)

    def test_rejection_diagnostics_check_and_web_list_do_not_fetch_source(self):
        state = ThresholdState(fail_image=True, pages={'19:1': ['101']})
        with fixture.fixture_server(state) as base:
            path = self.write_config(base, 2)
            self.cli(path)
            before = state.counts.copy()
            checked = json.loads(self.cli(path, '--check').stdout)
            self.assertEqual(checked['rejected'], 1)
            config = archive.Config.from_file(path)
            config.web_port = 0
            server = archive.make_web_server(config)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with urlopen(f'http://127.0.0.1:{server.server_port}/') as response:
                    listing = response.read().decode()
                with urlopen(f'http://127.0.0.1:{server.server_port}/api/debug/state') as response:
                    debug = json.load(response)
                self.assertNotIn('山间记录 101', listing)
                self.assertNotRegex(listing, r'href="/post/\d+')
                self.assertEqual(debug['rejected'], 1)
                self.assertEqual(debug['archives'], 0)
                self.assertEqual(state.counts, before)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)


LEGACY_SCHEMA = '''
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE archives(id INTEGER PRIMARY KEY,forum_key TEXT NOT NULL,tid TEXT NOT NULL,
 title TEXT NOT NULL,filename TEXT NOT NULL UNIQUE,sha256 TEXT NOT NULL,
 size INTEGER NOT NULL,archived_at TEXT NOT NULL,UNIQUE(forum_key,tid));
CREATE TABLE pending(forum_key TEXT NOT NULL,tid TEXT NOT NULL,title TEXT NOT NULL,
 filename TEXT NOT NULL UNIQUE,tmp_name TEXT NOT NULL,sha256 TEXT NOT NULL,
 size INTEGER NOT NULL,archived_at TEXT NOT NULL,PRIMARY KEY(forum_key,tid));
CREATE TRIGGER immutable_update BEFORE UPDATE ON archives
 BEGIN SELECT RAISE(ABORT,'successful archives are immutable'); END;
CREATE TRIGGER immutable_delete BEFORE DELETE ON archives
 BEGIN SELECT RAISE(ABORT,'successful archives are immutable'); END;
PRAGMA user_version=1;
'''


class ThresholdHistoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='threshold-history-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def config(self, minimum=2):
        return archive.Config('neutral-history', 'https://forum.invalid/index.php',
                              self.root / 'output', self.root / 'data/archive.sqlite3',
                              min_images=minimum)

    def store(self):
        store = archive.Store(self.config())
        self.addCleanup(store.close)
        return store

    def test_config_rejects_negative_boolean_fractional_or_string_thresholds(self):
        for value in ('-1', 'true', '1.5', '"2"'):
            with self.subTest(value=value):
                path = self.root / 'config.toml'
                path.write_text('[forum]\nkey="neutral"\nurl="https://forum.invalid/index.php"\n'
                                f'[archive]\nmin_images={value}\n')
                with self.assertRaises(archive.ArchiveError):
                    archive.Config.from_file(path)
        path.write_text('[forum]\nkey="neutral"\nurl="https://forum.invalid/index.php"\n')
        self.assertEqual(archive.Config.from_file(path).min_images, 0)

    def test_permanent_rejection_is_immutable_and_prevents_future_archives(self):
        store = self.store()
        store.reject('101', archive.InsufficientImages(1, 2))
        original = dict(store.rejection('101'))
        self.assertTrue(store.terminal('101'))
        self.assertIsNone(store.known('101'))
        store.reject('101', archive.InsufficientImages(0, 100))
        self.assertEqual(dict(store.rejection('101')), original)
        store.save('101', 'Must not be saved', b'not an archive')
        self.assertIsNone(store.known('101'))
        self.assertEqual(list(self.config().output_dir.glob('*.html')), [])
        for statement in ('UPDATE rejected SET reason="changed"', 'DELETE FROM rejected'):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                with store.db:
                    store.db.execute(statement)

    def test_rejection_cannot_replace_success_or_pending_history(self):
        store = self.store()
        store.save('101', 'Original', b'<p>Original fixture archive</p>')
        first = dict(store.known('101'))
        store.reject('101', archive.InsufficientImages(0, 100))
        self.assertIsNone(store.rejection('101'))
        self.assertEqual(dict(store.known('101')), first)
        with patch.object(store, 'finish', side_effect=RuntimeError('injected interruption')):
            with self.assertRaises(RuntimeError):
                store.save('102', 'Prepared', b'<p>Prepared fixture archive</p>')
        with self.assertRaises((archive.ArchiveError, sqlite3.IntegrityError)):
            store.reject('102', archive.InsufficientImages(0, 100))
        self.assertIsNone(store.rejection('102'))
        self.assertEqual(store.db.execute('SELECT count(*) FROM pending').fetchone()[0], 1)

    def test_rejection_is_scoped_to_stable_forum_identity(self):
        first = self.store()
        first.reject('101', archive.InsufficientImages(0, 2))
        first.close()
        second = archive.Store(replace(self.config(), forum_key='another-neutral-forum'))
        self.addCleanup(second.close)
        self.assertFalse(second.terminal('101'))
        second.save('101', 'Another forum original', b'<p>Different forum</p>')
        self.assertIsNotNone(second.known('101'))

    def test_rejection_only_history_loss_still_fails_closed(self):
        store = self.store()
        store.reject('101', archive.InsufficientImages(0, 2))
        store.close()
        for file in self.config().db_path.parent.glob('archive.sqlite3*'):
            file.unlink()
        with self.assertRaises(archive.ArchiveError):
            archive.Store(self.config())

    def test_v1_migration_preserves_success_identity_and_recovers_pending_without_network(self):
        config = self.config()
        config.output_dir.mkdir()
        config.db_path.parent.mkdir()
        identity = '0123456789abcdef0123456789abcdef'
        (config.output_dir / '.discuz-archive.json').write_text(json.dumps({'identity': identity, 'version': 1}))
        first = b'<p>First immutable legacy fixture</p>'
        prepared = b'<p>Prepared immutable legacy fixture</p>'
        (config.output_dir / 'First.html').write_bytes(first)
        (config.output_dir / '.pending-neutral.tmp').write_bytes(prepared)
        timestamp = '2026-11-03T00:00:00+00:00'
        with closing(sqlite3.connect(config.db_path)) as db:
            db.executescript(LEGACY_SCHEMA)
            db.execute('INSERT INTO meta VALUES(?,?)', ('identity', identity))
            db.execute('INSERT INTO archives VALUES(?,?,?,?,?,?,?,?)',
                       (1, config.forum_key, '101', 'First', 'First.html', hashlib.sha256(first).hexdigest(), len(first), timestamp))
            db.execute('INSERT INTO pending VALUES(?,?,?,?,?,?,?,?)',
                       (config.forum_key, '102', 'Prepared', 'Prepared.html', '.pending-neutral.tmp',
                        hashlib.sha256(prepared).hexdigest(), len(prepared), timestamp))
            db.commit()
        with patch.object(archive.Client, '__init__', side_effect=AssertionError('Migration must not access network')):
            store = archive.Store(config)
            self.addCleanup(store.close)
            self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0], 3)
            self.assertEqual(store.db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()[0], identity)
            self.assertEqual(store.known('101')['filename'], 'First.html')
            self.assertEqual(store.db.execute('SELECT count(*) FROM pending').fetchone()[0], 1)
            store.recover()
        self.assertEqual((config.output_dir / 'First.html').read_bytes(), first)
        self.assertEqual((config.output_dir / 'Prepared.html').read_bytes(), prepared)
        self.assertIsNotNone(store.known('102'))
        self.assertEqual(store.db.execute('SELECT count(*) FROM pending').fetchone()[0], 0)
        self.assertEqual(store.db.execute('SELECT count(*) FROM rejected').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
