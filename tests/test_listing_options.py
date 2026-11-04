"""Publication metadata and listing controls using neutral local files and HTTP."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import discuz_archive as archive
from tests.test_web_base_path import running_reader


class PageParser(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.forms, self.selects, self.inputs, self.links, self.policies = [], [], [], [], []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'form': self.forms.append(values)
        if tag == 'select': self.selects.append(values)
        if tag == 'input': self.inputs.append(values)
        if tag == 'a' and 'href' in values: self.links.append(values['href'])
        if tag == 'meta' and values.get('http-equiv', '').lower() == 'content-security-policy':
            self.policies.append(values.get('content', ''))


def listing_rows(source):
    rows = []
    for fragment in re.findall(r'<tr\b[^>]*>(.*?)</tr>', source, re.S):
        match = re.search(r'/post/(\d+)', fragment)
        if match:
            cells = [html.unescape(re.sub(r'<[^>]+>', '', cell)).strip()
                     for cell in re.findall(r'<td\b[^>]*>(.*?)</td>', fragment, re.S)]
            rows.append((int(match[1]), cells))
    return rows


class ListingOptionsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='discuz-listing-options-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = archive.Config('neutral-dates', 'https://forum.example.org/index.php',
                                     self.root/'output', self.root/'data/archive.sqlite3',
                                     web_host='127.0.0.1', web_port=0)

    def request(self, port, path, method='GET'):
        from http.client import HTTPConnection
        connection = HTTPConnection('127.0.0.1', port, timeout=5)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def payload(self, published=None):
        return archive.shell('Neutral dated article', '<p>Neutral first snapshot.</p>', published_at=published)

    def seed(self, dates):
        store = archive.Store(self.config)
        try:
            for index, published in enumerate(dates, 1):
                store.save(str(index), f'Neutral article {index}', self.payload(published))
        finally:
            store.close()

    def test_normalize_publication_preserves_precision_and_wall_clock(self):
        for original, expected in (
            ('2026-11-03', '2026-11-03'),
            ('发表于 2026-11-03 12:34', '2026-11-03 12:34'),
            ('2026-11-03 12:34:56', '2026-11-03 12:34:56'),
            ('2024-02-29 00:00', '2024-02-29 00:00'),
        ):
            with self.subTest(original=original):
                self.assertEqual(archive.normalize_publication(original), expected)
        for original in (None, '', '昨天 12:34', '3 小时前', '2026-02-29',
                         '2026-11-31 12:00', '2026-11-03 25:61', '11-03 12:34'):
            with self.subTest(invalid=original):
                self.assertIsNone(archive.normalize_publication(original))

    def test_extraction_prefers_absolute_span_title_over_relative_display(self):
        client = archive.Client(self.config)
        root = archive.Document(
            '<h1 id="thread_subject">Neutral original date</h1><div id="post_11">'
            '<em id="authorposton11">发表于 <span title="2024-01-02 03:04:05">昨天</span></em>'
            '<div id="postmessage_11"><p>Neutral main post.</p></div></div>'
            '<div id="post_12"><em id="authorposton12">2026-11-03 12:00</em>'
            '<div id="postmessage_12">Reply must not supply the original date.</div></div>'
        ).root
        _, payload = archive.extract_post(root, client, self.config.url+'?tid=11')
        document = archive.Document(payload.decode()).root
        metadata = [n.attrs.get('content') for n in document.walk()
                    if n.tag == 'meta' and n.attrs.get('name') == 'discuz-published-at']
        self.assertEqual(metadata, ['2024-01-02 03:04:05'])
        relative = archive.Document(
            '<h1 id="thread_subject">Neutral relative date</h1><div id="post_11">'
            '<em id="authorposton11">发表于 昨天 12:34</em>'
            '<div id="postmessage_11">Neutral content.</div></div>').root
        _, payload = archive.extract_post(relative, client, self.config.url+'?tid=11')
        self.assertNotIn(b'name="discuz-published-at"', payload)

    def test_saved_dates_are_immutable_and_recovered_with_pending_atomically(self):
        store = archive.Store(self.config)
        payload = self.payload('2023-08-09 10:11')
        with patch.object(store, 'finish', side_effect=RuntimeError('neutral interrupted commit')):
            with self.assertRaises(RuntimeError): store.save('1', 'Neutral pending date', payload)
        self.assertEqual(store.db.execute('SELECT count(*) FROM archive_dates').fetchone()[0], 0)
        store.close()
        recovered = archive.Store(self.config)
        try:
            with patch.object(archive.Client, '__init__', side_effect=AssertionError('No network during recovery')):
                recovered.recover()
            self.assertEqual(recovered.db.execute('SELECT published_at FROM archive_dates').fetchone()[0],
                             '2023-08-09 10:11')
            self.assertEqual(recovered.db.execute('PRAGMA user_version').fetchone()[0], 3)
            for statement in ('UPDATE archive_dates SET published_at="2026-11-03"', 'DELETE FROM archive_dates'):
                with self.assertRaises(sqlite3.IntegrityError):
                    with recovered.db: recovered.db.execute(statement)
            recovered.save('1', 'Later title', self.payload('2026-11-03'))
            self.assertEqual(recovered.db.execute('SELECT published_at FROM archive_dates').fetchone()[0],
                             '2023-08-09 10:11')
        finally:
            recovered.close()

    def test_unknown_dates_have_no_side_rows_and_are_last_in_both_directions(self):
        self.seed(['2024-01-01', None, '2026-11-03 12:00', '2024-01-01', None])
        with closing(sqlite3.connect(self.config.db_path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM archive_dates').fetchone()[0], 3)
        with running_reader(self.config) as port:
            for sort, expected in (('published_desc', [3,4,1,5,2]),
                                   ('published_asc', [4,1,3,5,2]),
                                   ('archived_desc', [5,4,3,2,1])):
                with self.subTest(sort=sort):
                    status, _, body = self.request(port, '/?'+urlencode({'sort':sort}))
                    self.assertEqual(status, 200)
                    text = body.decode()
                    self.assertIn('原始发帖时间', text)
                    self.assertIn('归档时间', text)
                    rows = listing_rows(text)
                    self.assertEqual([row[0] for row in rows], expected)
                    for identity, cells in rows:
                        self.assertGreaterEqual(len(cells), 3)
                        if identity in {2,5}: self.assertIn('未知', cells[1])

    def test_page_sizes_and_prefixed_navigation_preserve_selected_options(self):
        self.seed(['2024-01-01']*107)
        config = replace(self.config, web_base_path='/archive/nested')
        with running_reader(config) as port:
            for size in (20,50,100):
                with self.subTest(per_page=size):
                    query = dict(per_page=size, sort='published_asc')
                    status, _, body = self.request(port, '/archive/nested/?'+urlencode(query))
                    self.assertEqual(status, 200)
                    self.assertEqual(len(listing_rows(body.decode())), size)
                    links = PageParser(body.decode()).links
                    next_links = [link for link in links if parse_qs(urlsplit(link).query).get('page') == ['2']]
                    self.assertTrue(next_links)
                    for link in next_links:
                        self.assertEqual(urlsplit(link).path, '/archive/nested/')
                        params = parse_qs(urlsplit(link).query)
                        self.assertEqual(params['per_page'], [str(size)])
                        self.assertEqual(params['sort'], ['published_asc'])
                    status, _, body = self.request(port, next_links[0])
                    self.assertEqual(status, 200)
                    self.assertEqual(len(listing_rows(body.decode())), min(size,107-size))

    def test_toolbar_resets_page_and_listing_csp_allows_only_same_origin_form(self):
        self.seed(['2024-01-01'])
        config = replace(self.config, web_base_path='/archive')
        with running_reader(config) as port:
            status, headers, body = self.request(port, '/archive/?page=2&per_page=20&sort=published_asc')
            self.assertEqual(status, 200)
            parser = PageParser(body.decode())
            self.assertIn("form-action 'self'", headers['Content-Security-Policy'])
            self.assertTrue(parser.policies)
            self.assertTrue(all("form-action 'self'" in policy for policy in parser.policies))
            self.assertEqual(len(parser.forms), 1)
            self.assertEqual(parser.forms[0].get('method', '').lower(), 'get')
            self.assertEqual(parser.forms[0].get('action'), '/archive/')
            self.assertEqual({select['name'] for select in parser.selects}, {'per_page','sort'})
            for element in parser.inputs:
                if element.get('name') == 'page': self.assertEqual(element.get('value'), '1')
            status, headers, payload = self.request(port, '/archive/post/1')
            self.assertEqual(status, 200)
            self.assertIn("form-action 'none'", headers['Content-Security-Policy'])
            self.assertIn('sandbox', headers['Content-Security-Policy'])
            self.assertEqual(payload, self.payload('2024-01-01'))

    def test_invalid_listing_options_return_400(self):
        self.seed([None])
        with running_reader(self.config) as port:
            for query in ({'per_page':'0'}, {'per_page':'21'}, {'per_page':'1000'},
                          {'sort':'published_desc;DROP TABLE archives'}, {'sort':'unknown'},
                          {'page':'0'}, {'page':'-1'}, {'page':'1000001'}):
                with self.subTest(query=query):
                    status, _, _ = self.request(port, '/?'+urlencode(query))
                    self.assertEqual(status, 400)

    def legacy(self, version):
        self.config.output_dir.mkdir()
        self.config.db_path.parent.mkdir()
        payload = b'<!doctype html><meta name="discuz-published-at" content="2020-01-01"><p>Old neutral snapshot</p>'
        (self.config.output_dir/'old.html').write_bytes(payload)
        (self.config.output_dir/'.discuz-archive.json').write_text(json.dumps(dict(identity='neutral-legacy',version=1)))
        with closing(sqlite3.connect(self.config.db_path)) as db:
            db.executescript('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);'
                'CREATE TABLE archives(id INTEGER PRIMARY KEY,forum_key TEXT,tid TEXT,title TEXT,filename TEXT UNIQUE,sha256 TEXT,size INTEGER,archived_at TEXT,UNIQUE(forum_key,tid));'
                'CREATE TABLE pending(forum_key TEXT,tid TEXT,title TEXT,filename TEXT UNIQUE,tmp_name TEXT,sha256 TEXT,size INTEGER,archived_at TEXT,PRIMARY KEY(forum_key,tid));'
                + ('CREATE TABLE rejected(forum_key TEXT,tid TEXT,available_images INTEGER,min_images INTEGER,reason TEXT,rejected_at TEXT,PRIMARY KEY(forum_key,tid));' if version == 2 else '')
                + f'PRAGMA user_version={version};')
            db.execute('INSERT INTO meta VALUES(?,?)', ('identity','neutral-legacy'))
            db.execute('INSERT INTO archives VALUES(?,?,?,?,?,?,?,?)', (1,self.config.forum_key,'1','Neutral legacy article','old.html',
                       hashlib.sha256(payload).hexdigest(),len(payload),'2026-11-03T12:00:00+00:00'))
            db.commit()

    def test_v1_and_v2_readers_show_unknown_without_migrating_or_parsing_old_html(self):
        for version in (1,2):
            with self.subTest(version=version), tempfile.TemporaryDirectory(prefix='discuz-legacy-listing-') as directory:
                original = self.config
                root = Path(directory)
                self.config = replace(original, output_dir=root/'output', db_path=root/'data/archive.sqlite3')
                try:
                    self.legacy(version)
                    with running_reader(self.config) as port:
                        status, _, body = self.request(port, '/?sort=published_desc')
                    self.assertEqual(status, 200)
                    cells = listing_rows(body.decode())[0][1]
                    self.assertIn('未知', cells[1])
                    self.assertNotIn('2020-01-01', cells[1])
                    with closing(sqlite3.connect(self.config.db_path)) as db:
                        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], version)
                    migrated = archive.Store(self.config)
                    try:
                        self.assertEqual(migrated.db.execute('PRAGMA user_version').fetchone()[0], 3)
                        self.assertEqual(migrated.db.execute('SELECT count(*) FROM archive_dates').fetchone()[0], 0)
                        self.assertEqual(migrated.known('1')['title'], 'Neutral legacy article')
                    finally:
                        migrated.close()
                finally:
                    self.config = original


if __name__ == '__main__':
    unittest.main()
