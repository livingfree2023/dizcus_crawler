"""Neutral offline extraction regressions; no external forum is contacted.

The markup below deliberately models permission banners in different post floors.
It is synthetic test input and never part of production archives.
"""
from __future__ import annotations

import base64
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'discuz_archive.py'
SPEC = importlib.util.spec_from_file_location('discuz_archive_extraction_tests', SCRIPT)
archive = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = archive
SPEC.loader.exec_module(archive)

# A neutral one-pixel GIF is sufficient to verify image preservation here; the
# full HTTP suite separately checks multi-frame GIF byte-for-byte preservation.
GIF = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
IMAGE = 'data:image/gif;base64,' + base64.b64encode(GIF).decode('ascii')


class ExtractionPermissionTests(unittest.TestCase):
    def setUp(self):
        self.config = archive.Config(
            forum_key='neutral-extraction-fixture',
            url='https://forum.example.org/index.php',
            output_dir=Path('/unused-output'),
            db_path=Path('/unused-data/archive.sqlite3'),
        )
        self.client = archive.Client(self.config)
        self.url = 'https://forum.example.org/forum.php?mod=viewthread&tid=100'

    def document(self, *, main_notice='', reply_notice='', modern=True):
        message = '<p>首楼中性正文，必须完整保留。</p><img src="' + IMAGE + '" alt="中性图像">'
        if modern:
            # The permission notice is outside the inner pid table, but still
            # inside post_100. Choosing the nearer pid ancestor used to miss it.
            main = ('<div id="post_100"><strong>1#</strong>'
                    '<table id="pid100"><tr><td id="postmessage_100">' + message +
                    '</td></tr></table>' + main_notice + '</div>')
        else:
            main = ('<table id="pid100"><tr><td><strong>1#</strong>'
                    '<div id="postmessage_100">' + message + '</div>' +
                    main_notice + '</td></tr></table>')
        reply = ('<div id="post_200"><strong>2#</strong>'
                 '<div id="postmessage_200">REPLY MUST NOT APPEAR'
                 '<img src="https://reply.example.org/never-request.gif"></div>' +
                 reply_notice + '</div>')
        return archive.Document('<h1 id="thread_subject">中性权限回归样例</h1>' + main + reply).root

    def test_main_floor_permission_notice_outside_body_is_not_a_success(self):
        for modern in (True, False):
            for attributes in (
                'class="attach_nopermission attach_tips"',
                'id="attach_nopermission100"',
                'class="locked"',
                'class="showhide"',
            ):
                with self.subTest(modern=modern, attributes=attributes):
                    notice = '<div ' + attributes + '>需要额外权限查看主帖附件</div>'
                    root = self.document(main_notice=notice, modern=modern)
                    with patch.object(self.client, 'fetch', side_effect=AssertionError('unexpected network')) as fetch:
                        with self.assertRaises(archive.ArchiveError):
                            archive.extract_post(root, self.client, self.url)
                        fetch.assert_not_called()

    def test_reply_permission_notice_does_not_block_complete_main_floor(self):
        for modern in (True, False):
            for attributes in ('class="attach_nopermission attach_tips"', 'class="locked"'):
                with self.subTest(modern=modern, attributes=attributes):
                    root = self.document(reply_notice='<div ' + attributes +
                                         '>REPLY PERMISSION NOTICE</div>', modern=modern)
                    with patch.object(self.client, 'fetch', side_effect=AssertionError('reply media fetched')) as fetch:
                        title, data = archive.extract_post(root, self.client, self.url)
                        fetch.assert_not_called()
                    self.assertEqual(title, '中性权限回归样例')
                    self.assertIn('首楼中性正文，必须完整保留。'.encode(), data)
                    self.assertIn(IMAGE.encode(), data)
                    self.assertNotIn(b'REPLY MUST NOT APPEAR', data)
                    self.assertNotIn(b'REPLY PERMISSION NOTICE', data)
                    self.assertNotIn(b'never-request.gif', data)


class DigestListingTests(unittest.TestCase):
    """Stock DiscuzX hides digest badges while its digest filter is active.

    The caller must confirm the active server-rendered filter before setting
    digest_confirmed; merely putting filter=digest into a URL is insufficient.
    """
    base = 'https://forum.example.org/forum.php?mod=forumdisplay&fid=19&filter=digest&digest=1'

    def root(self, rows, *, active=False):
        classes = 'xi2 xw1' if active else 'xi2'
        return archive.Document(
            '<div id="threadlist"><div class="tf"><a class="' + classes +
            '" href="forum.php?mod=forumdisplay&amp;fid=19&amp;filter=digest&amp;digest=1">精华</a></div>'
            '<input type="hidden" name="listextra" value="page%3D1%26filter%3Ddigest%26digest%3D1">'
            '<table id="threadlisttableid" summary="forum_19">' + rows + '</table></div>'
        ).root

    def row(self, tid, *, badge='', sticky=False):
        prefix = 'stickthread_' if sticky else 'normalthread_'
        return ('<tbody id="' + prefix + str(tid) + '"><tr><th>' + badge +
                '<a class="s xst" href="forum.php?mod=viewthread&amp;tid=' + str(tid) +
                '">中性主题 ' + str(tid) + '</a></th></tr></tbody>')

    def test_confirmed_stock_filter_accepts_normal_rows_without_digest_badges(self):
        root = self.root(self.row(101) + self.row(102), active=True)
        self.assertEqual(list(archive.digest_threads(root, self.base, digest_confirmed=True)),
                         ['101', '102'])

    def test_request_url_alone_does_not_confirm_unmarked_rows(self):
        root = self.root(self.row(101), active=False)
        self.assertEqual(list(archive.digest_threads(root, self.base, digest_confirmed=False)), [])

    def test_explicit_digest_levels_and_sticky_badge_remain_accepted(self):
        rows = ''.join(self.row(100+level, badge='<img src="static/image/common/digest_' +
                               str(level) + '.gif" alt="digest">', sticky=(level == 3))
                       for level in (1, 2, 3))
        self.assertEqual(list(archive.digest_threads(self.root(rows), self.base, digest_confirmed=False)),
                         ['101', '102', '103'])

    def test_unconfirmed_non_digest_sticky_is_never_treated_as_digest(self):
        root = self.root(self.row(999, sticky=True), active=False)
        self.assertEqual(list(archive.digest_threads(root, self.base, digest_confirmed=False)), [])


if __name__ == '__main__':
    unittest.main()
