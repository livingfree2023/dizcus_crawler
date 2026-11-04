"""Bounded image concurrency against a real neutral localhost HTTP server."""
from __future__ import annotations

import base64
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

import discuz_archive as archive
from tests.test_discuz_archive import GIF


def image_bytes(index):
    # A valid GIF comment makes each neutral fixture unique without changing frames.
    comment = str(index).encode('ascii')
    return GIF[:-1] + b'\x21\xfe' + bytes([len(comment)]) + comment + b'\x00;'


@dataclass
class ImageState:
    active: int = 0
    max_active: int = 0
    counts: Counter = field(default_factory=Counter)
    cookie_required: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


@contextmanager
def image_server(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            with state.lock:
                state.counts[path] += 1
            if path == '/session':
                payload = b'<html><p>Neutral session ready</p></html>'
                self.send_response(200)
                self.send_header('Set-Cookie', 'neutral_session=ready; Path=/; HttpOnly')
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if not path.startswith('/image/'):
                self.send_error(404)
                return
            if state.cookie_required and 'neutral_session=ready' not in self.headers.get('Cookie', ''):
                self.send_error(403)
                return
            index = int(path.rsplit('/', 1)[1].split('.')[0])
            with state.lock:
                state.active += 1
                state.max_active = max(state.max_active, state.active)
            try:
                # Slow responses expose overlap without timing assertions on overall speed.
                time.sleep(0.12 + (0.03 if index % 2 == 0 else 0))
            finally:
                with state.lock:
                    state.active -= 1
            payload = image_bytes(index)
            self.send_response(200)
            self.send_header('Content-Type', 'image/gif')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


class ParallelImageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='discuz-parallel-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def config(self, base, workers=3, minimum=1):
        return archive.Config('neutral-parallel', base+'/index.php', self.root/'output',
                              self.root/'data/archive.sqlite3', image_workers=workers,
                              image_interval=0, interval=0, timeout=3, retries=0,
                              min_images=minimum, respect_robots=False)

    def document(self, count, *, duplicate=False, excluded=False):
        content = ''.join(f'<img src="/image/{n}.gif" alt="picture-{n}">' for n in range(count))
        if duplicate:
            content += '<img src="/image/0.gif#repeated" alt="duplicate">'
        if excluded:
            content += ('<div class="a_pr"><img src="/image/91.gif"></div>'
                        '<div class="locked"><img src="/image/92.gif"></div>')
        return archive.Document('<h1 id="thread_subject">Neutral parallel test</h1>'
            '<div id="post_1"><div id="postmessage_1">'+content+'</div>'
            '<div class="sign"><img src="/image/93.gif"></div></div>'
            '<div id="post_2"><div id="postmessage_2"><img src="/image/94.gif"></div></div>').root

    def test_multiple_image_requests_overlap_within_configured_bound(self):
        state = ImageState()
        with image_server(state) as base:
            client = archive.Client(self.config(base, workers=3, minimum=8))
            title, payload = archive.extract_post(self.document(8), client, base+'/viewthread.php?tid=1')
        self.assertEqual(title, 'Neutral parallel test')
        self.assertGreaterEqual(state.max_active, 2)
        self.assertLessEqual(state.max_active, 3)
        text = payload.decode()
        positions = [text.index(f'alt="picture-{n}"') for n in range(8)]
        self.assertEqual(positions, sorted(positions), 'Completion order must not change article order')
        for n in range(8):
            self.assertEqual(state.counts[f'/image/{n}.gif'], 1)
            self.assertIn(base64.b64encode(image_bytes(n)), payload)

    def test_single_worker_stays_serial_and_duplicate_source_is_fetched_once(self):
        state = ImageState()
        with image_server(state) as base:
            client = archive.Client(self.config(base, workers=1, minimum=4))
            _, payload = archive.extract_post(self.document(4, duplicate=True), client, base+'/viewthread.php?tid=1')
        self.assertEqual(state.max_active, 1)
        self.assertEqual(state.counts['/image/0.gif'], 1)
        self.assertIn(b'alt="duplicate"', payload)
        self.assertEqual(sum(state.counts.values()), 4)

    def test_worker_requests_reuse_the_authenticated_cookie_session(self):
        state = ImageState(cookie_required=True)
        with image_server(state) as base:
            client = archive.Client(self.config(base, workers=3, minimum=4))
            client.page(base+'/session')
            _, payload = archive.extract_post(self.document(4), client, base+'/viewthread.php?tid=1')
        self.assertGreaterEqual(state.max_active, 2)
        for n in range(4):
            self.assertIn(base64.b64encode(image_bytes(n)), payload)

    def test_prefetch_excludes_ads_permissions_signatures_and_replies(self):
        state = ImageState()
        with image_server(state) as base:
            client = archive.Client(self.config(base, workers=3, minimum=3))
            _, payload = archive.extract_post(self.document(3, excluded=True), client, base+'/viewthread.php?tid=1')
        for n in (91, 92, 93, 94):
            self.assertEqual(state.counts[f'/image/{n}.gif'], 0)
            self.assertNotIn(base64.b64encode(image_bytes(n)), payload)
        self.assertEqual(sum(state.counts.values()), 3)

    def test_parallel_configuration_bounds_and_zero_interval_defaults(self):
        path = self.root/'config.toml'
        prefix = '[forum]\nkey="neutral"\nurl="https://forum.example.org/index.php"\n[request]\ninterval=0\n'
        for value in ('0', '9', 'true', '2.5'):
            with self.subTest(workers=value):
                path.write_text(prefix+f'image_workers={value}\n')
                with self.assertRaises(archive.ArchiveError):
                    archive.Config.from_file(path)
        for value in ('-0.1', 'nan', 'inf'):
            with self.subTest(image_interval=value):
                path.write_text(prefix+f'image_interval={value}\n')
                with self.assertRaises(archive.ArchiveError):
                    archive.Config.from_file(path)
        path.write_text(prefix)
        config = archive.Config.from_file(path)
        self.assertEqual(config.image_workers, 4)
        self.assertEqual(config.image_interval, 0)


if __name__ == '__main__':
    unittest.main()
