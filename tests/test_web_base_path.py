"""Neutral real-HTTP regressions for an authenticated reverse-proxy subpath."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import replace
from http.client import HTTPConnection
import json
from pathlib import Path
import re
import secrets
import tempfile
import threading
import unittest

import discuz_archive as archive


@contextmanager
def running_reader(config):
    server = archive.make_web_server(config)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


class WebBasePathTests(unittest.TestCase):
    prefix = '/dizcus_crawler'

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='discuz-web-prefix-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        token = secrets.token_urlsafe(32)
        self.authorization = 'Basic '+base64.b64encode(('archive:'+token).encode()).decode()
        token_file = self.root/'web-token.txt'
        token_file.write_text(token)
        self.config = archive.Config(
            'neutral-prefix', 'https://forum.example.org/index.php', self.root/'output',
            self.root/'data/archive.sqlite3', web_host='127.0.0.1', web_port=0,
            web_token_file=token_file, web_base_path=self.prefix,
        )
        self.payload = b'<!doctype html><html><title>Neutral snapshot</title><p>Original bytes.</p></html>'

    def seed(self, count=1):
        store = archive.Store(self.config)
        try:
            for index in range(count):
                store.save(str(index+1), f'Neutral archived post {index+1}', self.payload)
        finally:
            store.close()

    def request(self, port, path, *, authorized=True, method='GET'):
        connection = HTTPConnection('127.0.0.1', port, timeout=5)
        headers = {'Authorization':self.authorization} if authorized else {}
        try:
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_exact_prefix_redirects_before_authentication_and_preserves_query(self):
        with running_reader(self.config) as port:
            status, headers, body = self.request(port, self.prefix+'?page=2', authorized=False)
        self.assertEqual(status, 308)
        self.assertEqual(headers['Location'], self.prefix+'/?page=2')
        self.assertNotIn(b'Neutral snapshot', body)
        self.assertNotIn('WWW-Authenticate', headers)

    def test_prefix_home_requires_auth_and_bare_root_remains_accessible(self):
        self.seed()
        with running_reader(self.config) as port:
            status, headers, _ = self.request(port, self.prefix+'/', authorized=False)
            self.assertEqual(status, 401)
            self.assertIn('Basic', headers['WWW-Authenticate'])
            status, _, body = self.request(port, self.prefix+'/')
            self.assertEqual(status, 200)
            text = body.decode()
            self.assertIn('Neutral archived post 1', text)
            self.assertIn(f'href="{self.prefix}/post/1"', text)
            self.assertIn(f'href="{self.prefix}/manifest.webmanifest"', text)
            self.assertIn(f'src="{self.prefix}/app.js"', text)
            status, _, _ = self.request(port, '/')
            self.assertEqual(status, 200)
            status, _, state = self.request(port, self.prefix+'/api/debug/state')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(state)['archives'], 1)
            status, _, _ = self.request(port, self.prefix+'/api/debug/state', authorized=False)
            self.assertEqual(status, 401)

    def test_prefixed_archive_preserves_original_bytes_head_and_download(self):
        self.seed()
        with running_reader(self.config) as port:
            for path in (self.prefix+'/post/1', self.prefix+'/post/1?download=1'):
                status, headers, body = self.request(port, path)
                self.assertEqual(status, 200)
                self.assertEqual(body, self.payload)
                self.assertEqual(int(headers['Content-Length']), len(self.payload))
                if path.endswith('download=1'):
                    self.assertTrue(headers['Content-Disposition'].startswith('attachment'))
            status, headers, body = self.request(port, self.prefix+'/post/1', method='HEAD')
            self.assertEqual(status, 200)
            self.assertEqual(body, b'')
            self.assertEqual(int(headers['Content-Length']), len(self.payload))
            status, _, _ = self.request(port, self.prefix+'/post/1', authorized=False)
            self.assertEqual(status, 401)

    def test_pagination_preserves_prefix(self):
        self.seed(51)
        with running_reader(self.config) as port:
            status, _, body = self.request(port, self.prefix+'/')
            self.assertEqual(status, 200)
            self.assertIn(f'href="{self.prefix}/?page=2"'.encode(), body)
            links = re.findall(rb'href="([^\"]+/post/\d+)"', body)
            self.assertEqual(len(links), 50)
            status, _, body = self.request(port, self.prefix+'/?page=2')
            self.assertEqual(status, 200)
            self.assertIn(f'href="{self.prefix}/?page=1"'.encode(), body)
            self.assertEqual(len(re.findall(rb'href="([^\"]+/post/\d+)"', body)), 1)

    def test_manifest_service_worker_and_assets_stay_within_prefix(self):
        with running_reader(self.config) as port:
            status, _, payload = self.request(port, self.prefix+'/manifest.webmanifest')
            self.assertEqual(status, 200)
            manifest = json.loads(payload)
            self.assertEqual(manifest['start_url'], self.prefix+'/')
            self.assertEqual(manifest['scope'], self.prefix+'/')
            self.assertTrue(all(icon['src'].startswith(self.prefix+'/') for icon in manifest['icons']))
            status, headers, source = self.request(port, self.prefix+'/sw.js')
            self.assertEqual(status, 200)
            self.assertEqual(headers['Service-Worker-Allowed'], self.prefix+'/')
            script = source.decode()
            self.assertIn(self.prefix+'/offline', script)
            self.assertIn(self.prefix+'/icon.svg', script)
            self.assertNotRegex(script, r"startsWith\(\s*['\"]discuz-shell-['\"]\s*\)")
            status, _, app = self.request(port, self.prefix+'/app.js')
            self.assertEqual(status, 200)
            self.assertIn(self.prefix+'/sw.js', app.decode())
            self.assertIn('scope', app.decode())
            for path in ('/offline', '/icon.svg'):
                status, _, _ = self.request(port, self.prefix+path)
                self.assertEqual(status, 200)
                status, _, _ = self.request(port, self.prefix+path, authorized=False)
                self.assertEqual(status, 401)
        with running_reader(replace(self.config, web_base_path='/another-archive')) as port:
            status, _, other_source = self.request(port, '/another-archive/sw.js')
            self.assertEqual(status, 200)
            self.assertNotEqual(source, other_source)

    def test_neighboring_prefix_and_unsafe_paths_do_not_resolve_to_files(self):
        self.seed()
        with running_reader(self.config) as port:
            for path in (self.prefix+'-other/', self.prefix+'/../config.toml',
                         self.prefix+'/post/../config.toml', self.prefix+'/post/9999',
                         self.prefix+'/data/archive.sqlite3'):
                with self.subTest(path=path):
                    status, _, _ = self.request(port, path)
                    self.assertEqual(status, 404)

    def test_config_base_path_normalization_and_invalid_values(self):
        path = self.root/'config.toml'
        header = '[forum]\nkey="neutral"\nurl="https://forum.example.org/index.php"\n[web]\n'
        for value, expected in (('', ''), ('/', ''), ('/dizcus_crawler/', '/dizcus_crawler'),
                                ('/archive/nested-v2_3/', '/archive/nested-v2_3')):
            with self.subTest(valid=value):
                path.write_text(header+'base_path='+json.dumps(value)+'\n')
                self.assertEqual(archive.Config.from_file(path).web_base_path, expected)
        for value in ('archive', '//archive', '/archive//nested', '/archive/..', '/archive/../other',
                      '/archive?x=1', '/archive#x', '/%61rchive', 'https://example.org/archive',
                      True, 1, ['/archive']):
            with self.subTest(invalid=value):
                encoded = ('true' if value is True else json.dumps(value))
                path.write_text(header+'base_path='+encoded+'\n')
                with self.assertRaises(archive.ArchiveError):
                    archive.Config.from_file(path)


if __name__ == '__main__':
    unittest.main()
