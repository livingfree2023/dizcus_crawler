"""Controlled neutral Discuz fixtures served over actual localhost HTTP.

These fixtures are intentionally synthetic test inputs. They are never included in
production archives or represented as external-site integration verification.
Run with: python3 -m unittest discover -v
"""

from __future__ import annotations

import base64
from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from email.message import Message
import hashlib
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

import discuz_archive as archive


SCRIPT = Path(__file__).resolve().parents[1] / "discuz_archive.py"
GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
    b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
    b"\x21\xf9\x04\x00\x0a\x00\x00\x00"
    b"\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00"
    b"\x21\xf9\x04\x00\x0a\x00\x00\x00"
    b"\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x4c\x01\x00\x3b"
)
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a4i0AAAAASUVORK5CYII="
)


@dataclass
class FixtureState:
    modern: bool = False
    edited: bool = False
    fail_image: bool = False
    poison_image: bool = False
    login_only: bool = False
    stock_digest_mode: bool = False
    digest_active: bool = True
    include_notice: bool = True
    include_digest_sticky: bool = False
    same_titles: bool = False
    charset: str = "utf-8"
    prefix: str = "/community/"
    counts: Counter = field(default_factory=Counter)
    pages: dict[str, list[str]] = field(
        default_factory=lambda: {"19:1": ["101"], "19:2": ["102"], "20:1": ["103"]}
    )

    def thread_url(self, tid: str) -> str:
        if self.modern:
            return f"forum.php?mod=viewthread&tid={tid}"
        return f"viewthread.php?tid={tid}"

    def forum_url(self, fid: str, page: int = 1) -> str:
        if self.modern:
            return f"forum.php?mod=forumdisplay&fid={fid}&filter=digest&digest=1&page={page}"
        return f"forumdisplay.php?fid={fid}&filter=digest&page={page}"

    def title(self, tid: str) -> str:
        if self.same_titles:
            return "相同标题：山间记录"
        if self.edited:
            return f"EDITED TITLE {tid}"
        return f"山间记录 {tid}"

    def home(self) -> str:
        return (
            '<!doctype html><html><head><title>Neutral fixture forum</title></head><body>'
            '<h1>Neutral Discuz test fixture</h1>'
            f'<a href="{escape(self.forum_url("19"))}">自然观察</a>'
            f'<a href="{escape(self.forum_url("20"))}">旅行记录</a>'
            # Duplicate discovery link must not double the board traversal.
            f'<a href="{escape(self.forum_url("19"))}">自然观察再次链接</a>'
            '</body></html>'
        )

    def board(self, fid: str, page: int, *, digest: bool = True) -> str:
        rows = []
        show_digest_icons = not (self.stock_digest_mode and digest)
        for tid in self.pages.get(f"{fid}:{page}", []):
            icon = '<img src="static/image/common/digest_1.gif" alt="精华" />' if show_digest_icons else ''
            rows.append(
                f'<tbody id="normalthread_{tid}"><tr><td>'
                f'{icon}'
                f'</td><th class="new"><a class="s xst" href="{escape(self.thread_url(tid))}">'
                f'{escape(self.title(tid))}</a></th></tr></tbody>'
            )
        # Global sticky and navigation links must not turn into archived threads.
        if self.include_notice:
            rows.append(
                '<tbody id="stickthread_999"><tr><td><img src="static/image/common/pin_1.gif" alt="置顶"></td>'
                f'<th><a href="{escape(self.thread_url("999"))}">FORUM NOTICE NOT DIGEST</a></th></tr></tbody>'
            )
        if self.include_digest_sticky:
            icon = '<img src="static/image/common/digest_2.gif" alt="精华" />' if show_digest_icons else ''
            rows.append(
                f'<tbody id="stickthread_104"><tr><td>{icon}</td>'
                f'<th><a href="{escape(self.thread_url("104"))}">真正的精华置顶</a></th></tr></tbody>'
            )
        pages = ""
        if f"{fid}:{page + 1}" in self.pages:
            pages = (
                '<div class="pages pg">'
                f'<a class="nxt" href="{escape(self.forum_url(fid, page + 1))}">下一页</a>'
                '</div>'
            )
        active_tab = ''
        if self.stock_digest_mode and digest and self.digest_active:
            active_tab = f'<a class="xi2 xw1" href="{escape(self.forum_url(fid))}">精华</a>'
        return f'<!doctype html><html><body>{active_tab}<table id="threadlisttableid">{"".join(rows)}</table>{pages}</body></html>'

    def thread(self, tid: str) -> str:
        if self.login_only:
            return '<!doctype html><html><head><title>请先登录</title></head><body><form>无权阅读此主题</form></body></html>'
        title = escape(self.title(tid))
        message = "EDITED BODY MUST NEVER REPLACE THE ARCHIVE" if self.edited else f"首次正文 {tid}：山路与晨光。"
        body = (
            f'<p>{message}</p><p><strong>保留加粗</strong> 与 <em>保留强调</em>。</p>'
            '<blockquote>保留正文引用。</blockquote>'
            '<img src="media/photo.png" alt="原始静态图片" />'
            '<img src="static/image/common/none.gif" file="media/motion.gif" alt="原始动图" />'
            '<div class="a_pr"><a href="https://ads.invalid/">ADVERTISEMENT TEXT</a>'
            '<img src="media/ad.gif" alt="广告" /></div>'
            '<div id="recommend_add">UNRELATED LIKE BUTTON</div>'
            '<div class="share"><a href="home.php?mod=spacecp&ac=favorite">UNRELATED FAVORITE</a></div>'
            '<script>UNTRUSTED_SCRIPT()</script>'
            '<iframe src="https://ads.invalid/frame"></iframe>'
            '<a href="javascript:UNTRUSTED_URL()">正文安全链接文字</a>'
            '<p onclick="UNTRUSTED_EVENT()">安全正文尾段。</p>'
        )
        if self.modern:
            posts = (
                '<div id="postlist">'
                '<div id="post_1001"><table id="pid1001"><tr>'
                '<td class="pls"><a class="xw1" href="home.php?mod=space&uid=7">示例作者</a></td>'
                '<td class="plc"><div class="pi"><strong><a>1#</a></strong>'
                '<div class="authi"><em id="authorposton1001">发表于 2026-11-03 10:00</em></div></div>'
                f'<div class="pct"><div class="pcb"><div class="t_fsz"><table><tr><td class="t_f" id="postmessage_1001">{body}</td></tr></table></div></div></div>'
                '<div class="sign">SIGNATURE MUST NOT APPEAR</div>'
                '<div class="pob">POST ACTIONS SHARE FAVORITE</div></td></tr></table></div>'
                '<div id="post_1002"><table id="pid1002"><tr><td class="plc">'
                '<td class="t_f" id="postmessage_1002">REPLY MUST NOT APPEAR'
                '<img src="media/reply.gif"></td></td></tr></table></div></div>'
            )
            heading = f'<h1 class="ts"><span id="thread_subject">{title}</span></h1>'
        else:
            posts = (
                '<div id="postlist"><table id="pid1001" summary="pid1001"><tr>'
                '<td class="postauthor"><a href="space.php?uid=7">示例作者</a><p>AUTHOR LEVEL AND AVATAR</p></td>'
                '<td class="postcontent"><div class="postinfo"><strong>1#</strong>'
                '<em id="authorposton1001">发表于 2026-11-03 10:00</em></div>'
                f'<div class="postmessage defaultpost"><div id="postmessage_1001">{body}</div></div>'
                '<div class="signatures">SIGNATURE MUST NOT APPEAR</div>'
                '<div class="postactions">POST ACTIONS SHARE FAVORITE</div></td></tr></table>'
                '<table id="pid1002"><tr><td class="postcontent">'
                '<div id="postmessage_1002">REPLY MUST NOT APPEAR<img src="media/reply.gif"></div>'
                '</td></tr></table></div>'
            )
            heading = f'<h1 id="thread_subject">{title}</h1>'
        return (
            f'<!doctype html><html><head><meta charset="{self.charset}"><title>{title} - Discuz!</title></head>'
            f'<body><nav>FORUM NAVIGATION</nav>{heading}{posts}'
            '<footer>POWERED BY DISCUZ UNRELATED FOOTER</footer></body></html>'
        )


@contextmanager
def fixture_server(state: FixtureState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            path = parsed.path.removeprefix(state.prefix)
            state.counts[f"path:{path}"] += 1
            payload: bytes | str
            media_type = f"text/html; charset={state.charset}"
            status = 200
            if path in {"index.php", ""} or (path == "forum.php" and not query):
                state.counts["home"] += 1
                payload = state.home()
            elif path == "forumdisplay.php" or (path == "forum.php" and query.get("mod") == ["forumdisplay"]):
                fid = query.get("fid", ["19"])[0]
                page = int(query.get("page", ["1"])[0])
                state.counts[f"board:{fid}:{page}"] += 1
                digest = query.get("filter") == ["digest"]
                state.counts[f"board-filter:{'digest' if digest else 'ordinary'}:{fid}:{page}"] += 1
                payload = state.board(fid, page, digest=digest)
            elif path == "viewthread.php" or (path == "forum.php" and query.get("mod") == ["viewthread"]):
                tid = query.get("tid", [""])[0]
                state.counts[f"thread:{tid}"] += 1
                payload = state.thread(tid)
            elif path == "media/photo.png":
                payload, media_type = PNG, "image/png"
            elif path == "media/motion.gif":
                if state.fail_image:
                    status, payload, media_type = 503, b"not available", "text/plain"
                elif state.poison_image:
                    payload, media_type = b"<html><body>Not a real GIF</body></html>", "image/gif"
                else:
                    # Real forums sometimes serve attachments as binary data.
                    payload, media_type = GIF, "application/octet-stream"
            elif path.startswith("media/") or path.startswith("static/"):
                payload, media_type = GIF, "image/gif"
            elif path == "robots.txt":
                payload, media_type = "User-agent: *\nAllow: /\n", "text/plain"
            else:
                status, payload, media_type = 404, "not found", "text/plain"
            if isinstance(payload, str):
                payload = payload.encode(state.charset)
            self.send_response(status)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}{state.prefix}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


class CrawlHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="discuz-neutral-test-")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def write_config(self, base: str, *, fids: list[int] | None = None, port: int = 8080) -> Path:
        path = self.root / "config.toml"
        path.write_text(
            '[forum]\nkey = "neutral-fixture"\n'
            f'url = "{base}index.php"\nfids = {fids or []}\n'
            '[archive]\noutput_dir = "output"\ndatabase = "data/archive.sqlite3"\n'
            '[request]\ninterval = 0\ntimeout = 3\nretries = 0\n'
            '[web]\nhost = "127.0.0.1"\n'
            f'port = {port}\n'
            '[schedule]\non_calendar = "*-*-* 03:00:00"\n',
            encoding="utf-8",
        )
        return path

    def cli(self, config: Path, *extra: str, expected: int | None = 0) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--config", str(config), *extra],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if expected is not None:
            self.assertEqual(result.returncode, expected, result.stdout + "\n" + result.stderr)
        return result

    def records(self) -> list[sqlite3.Row]:
        path = self.root / "data/archive.sqlite3"
        self.assertTrue(path.is_file(), "History database must be persisted")
        with closing(sqlite3.connect(path)) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute("SELECT * FROM archives ORDER BY tid").fetchall()

    def assert_clean_archive(self, body: str):
        for forbidden in (
            "REPLY MUST NOT APPEAR", "SIGNATURE MUST NOT APPEAR", "ADVERTISEMENT TEXT",
            "UNRELATED LIKE BUTTON", "UNRELATED FAVORITE", "POST ACTIONS SHARE FAVORITE",
            "FORUM NAVIGATION", "AUTHOR LEVEL AND AVATAR", "UNTRUSTED_SCRIPT",
            "UNTRUSTED_EVENT", "POWERED BY DISCUZ UNRELATED FOOTER",
            "UNTRUSTED_URL",
        ):
            self.assertNotIn(forbidden, body)
        self.assertNotRegex(body, r"(?i)<(?:script|iframe|object|embed)\b")
        self.assertNotRegex(body, r'(?i)\b(?:src|srcset)\s*=\s*["\']\s*(?:https?:|//)')
        self.assertIn(base64.b64encode(GIF).decode(), body, "The original multi-frame GIF bytes must be embedded")
        self.assertIn("保留加粗", body)
        self.assertIn("保留正文引用", body)

    def test_classic_discovers_boards_and_digest_pagination(self):
        state = FixtureState()
        with fixture_server(state) as base:
            config = self.write_config(base)
            self.cli(config)
        rows = self.records()
        self.assertEqual({str(row["tid"]) for row in rows}, {"101", "102", "103"})
        self.assertEqual(state.counts["thread:999"], 0, "A sticky announcement is not a digest thread")
        self.assertEqual(state.counts["board:19:2"], 1)
        for row in rows:
            path = self.root / "output" / row["filename"]
            body = path.read_text(encoding="utf-8")
            self.assert_clean_archive(body)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), row["sha256"])
            self.assertEqual(path.stat().st_size, row["size"])
        for unwanted in ("media/reply.gif", "media/ad.gif", "static/image/common/none.gif"):
            self.assertEqual(state.counts[f"path:{unwanted}"], 0, unwanted)

    def test_discuz_x_and_gbk_first_post(self):
        state = FixtureState(modern=True, charset="gbk", pages={"19:1": ["101"]})
        with fixture_server(state) as base:
            self.cli(self.write_config(base, fids=[19]))
        rows = self.records()
        self.assertEqual(len(rows), 1)
        body = (self.root / "output" / rows[0]["filename"]).read_text(encoding="utf-8")
        self.assertIn("山间记录 101", body)
        self.assertIn("首次正文 101", body)
        self.assert_clean_archive(body)

    def test_success_is_immutable_and_skipped_before_thread_requests(self):
        state = FixtureState(pages={"19:1": ["101"]})
        with fixture_server(state) as base:
            config = self.write_config(base, fids=[19])
            self.cli(config)
            initial = self.records()[0]
            archive = self.root / "output" / initial["filename"]
            original = archive.read_bytes()
            first_counts = state.counts.copy()
            state.edited = True
            self.cli(config)
            self.assertEqual(state.counts["thread:101"], first_counts["thread:101"])
            for media in ("media/photo.png", "media/motion.gif"):
                self.assertEqual(state.counts[f"path:{media}"], first_counts[f"path:{media}"])
            self.assertEqual(archive.read_bytes(), original)
            self.assertEqual(dict(self.records()[0]), dict(initial))
            self.assertEqual(len(list((self.root / "output").glob("*.html"))), 1)

    def test_missing_success_file_is_not_refetched(self):
        state = FixtureState(pages={"19:1": ["101"]})
        with fixture_server(state) as base:
            config = self.write_config(base, fids=[19])
            self.cli(config)
            row = self.records()[0]
            archive = self.root / "output" / row["filename"]
            archive.unlink()
            before = state.counts["thread:101"]
            self.cli(config, expected=None)
            self.assertEqual(state.counts["thread:101"], before)
            self.assertFalse(archive.exists())
            self.assertNotEqual(self.cli(config, "--check", expected=None).returncode, 0)
            self.assertEqual(len(self.records()), 1)

    def test_missing_history_with_existing_archives_fails_closed(self):
        state = FixtureState(pages={"19:1": ["101"]})
        with fixture_server(state) as base:
            config = self.write_config(base, fids=[19])
            self.cli(config)
            # Simulate loss of all history database files, not an intended reset.
            for path in (self.root / "data").glob("archive.sqlite3*"):
                path.unlink()
            before = state.counts["thread:101"]
            result = self.cli(config, expected=None)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(state.counts["thread:101"], before)
            self.assertEqual(len(list((self.root / "output").glob("*.html"))), 1)

    def test_failed_image_is_retryable_and_never_committed(self):
        state = FixtureState(fail_image=True, pages={"19:1": ["101"]})
        with fixture_server(state) as base:
            config = self.write_config(base, fids=[19])
            self.cli(config, expected=1)
            self.assertEqual(self.records(), [])
            self.assertEqual(list((self.root / "output").glob("*.html")), [])
            state.fail_image = False
            self.cli(config)
            self.assertEqual(len(self.records()), 1)
            self.assertEqual(state.counts["thread:101"], 2)

    def test_same_titles_do_not_overwrite_each_other(self):
        state = FixtureState(same_titles=True, pages={"19:1": ["101", "102"]})
        with fixture_server(state) as base:
            self.cli(self.write_config(base, fids=[19]))
        rows = self.records()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row["filename"] for row in rows}), 2)
        for row in rows:
            path = self.root / "output" / row["filename"]
            self.assertEqual(path.parent, self.root / "output")
            self.assertIn(f'首次正文 {row["tid"]}', path.read_text(encoding="utf-8"))

    def test_html_response_mislabeled_as_gif_is_not_archived(self):
        state = FixtureState(poison_image=True, pages={"19:1": ["101"]})
        with fixture_server(state) as base:
            self.cli(self.write_config(base, fids=[19]), expected=1)
        self.assertEqual(self.records(), [])
        self.assertEqual(list((self.root / "output").glob("*.html")), [])

    def test_login_page_is_not_mistaken_for_successful_thread(self):
        state = FixtureState(login_only=True, pages={"19:1": ["101"]})
        with fixture_server(state) as base:
            self.cli(self.write_config(base, fids=[19]), expected=1)
        self.assertEqual(self.records(), [])
        self.assertEqual(list((self.root / "output").glob("*.html")), [])

    def test_explicit_board_selection_does_not_archive_other_boards(self):
        state = FixtureState()
        with fixture_server(state) as base:
            self.cli(self.write_config(base, fids=[20]))
        self.assertEqual({str(row["tid"]) for row in self.records()}, {"103"})
        self.assertEqual(state.counts["thread:101"], 0)
        self.assertEqual(state.counts["thread:102"], 0)

    def test_stock_digest_filter_accepts_unmarked_normal_threads(self):
        state = FixtureState(
            modern=True, stock_digest_mode=True, include_notice=False,
            pages={"19:1": ["101"], "19:2": ["102"]},
        )
        with fixture_server(state) as base:
            self.cli(self.write_config(base, fids=[19]))
        self.assertEqual({str(row["tid"]) for row in self.records()}, {"101", "102"})
        self.assertEqual(state.counts["board-filter:digest:19:1"], 1)
        self.assertEqual(state.counts["board-filter:digest:19:2"], 1)
        self.assertEqual(state.counts["board-filter:ordinary:19:1"], 0)

    def test_stock_digest_sticky_is_verified_once_and_known_sticky_skips_verification(self):
        state = FixtureState(
            modern=True, stock_digest_mode=True, include_notice=False, include_digest_sticky=True,
            pages={"19:1": ["101"], "19:2": ["102"]},
        )
        with fixture_server(state) as base:
            config = self.write_config(base, fids=[19])
            self.cli(config)
            self.assertEqual({str(row["tid"]) for row in self.records()}, {"101", "102", "104"})
            self.assertEqual(state.counts["board-filter:ordinary:19:1"], 1)
            self.assertEqual(state.counts["thread:104"], 1)
            before = state.counts.copy()
            state.edited = True
            self.cli(config)
            self.assertEqual(state.counts["board-filter:ordinary:19:1"], before["board-filter:ordinary:19:1"])
            for tid in ("101", "102", "104"):
                self.assertEqual(state.counts[f"thread:{tid}"], before[f"thread:{tid}"])

    def test_stock_unverified_notice_is_not_archived_and_reports_partial_failure(self):
        state = FixtureState(
            modern=True, stock_digest_mode=True, include_notice=True, include_digest_sticky=True,
            pages={"19:1": ["101"], "19:2": ["102"]},
        )
        with fixture_server(state) as base:
            self.cli(self.write_config(base, fids=[19]), expected=1)
        self.assertEqual({str(row["tid"]) for row in self.records()}, {"101", "102", "104"})
        self.assertEqual(state.counts["thread:999"], 0)
        self.assertEqual(state.counts["board-filter:ordinary:19:1"], 1)
        with closing(sqlite3.connect(self.root / "data/archive.sqlite3")) as database:
            stats = json.loads(database.execute("SELECT value FROM meta WHERE key='last_run'").fetchone()[0])
        self.assertEqual(stats["failed"], 1, "An unresolved sticky is reported once even across listing pages")
        self.assertEqual(stats["archived"], 3)

    def test_stock_ignored_digest_filter_cannot_report_empty_success(self):
        state = FixtureState(
            modern=True, stock_digest_mode=True, digest_active=False, include_notice=False,
            pages={"19:1": ["101"]},
        )
        with fixture_server(state) as base:
            self.cli(self.write_config(base, fids=[19]), expected=1)
        self.assertEqual(self.records(), [])
        self.assertEqual(state.counts["thread:101"], 0)
        self.assertEqual(state.counts["board-filter:ordinary:19:1"], 0)

    def test_check_detects_local_changes_without_contacting_source(self):
        state = FixtureState(pages={"19:1": ["101"]})
        with fixture_server(state) as base:
            config = self.write_config(base, fids=[19])
            self.cli(config)
            before = state.counts.copy()
            self.cli(config, "--check")
            row = self.records()[0]
            archive = self.root / "output" / row["filename"]
            archive.write_text("local corruption", encoding="utf-8")
            self.assertNotEqual(self.cli(config, "--check", expected=None).returncode, 0)
            self.assertEqual(state.counts, before)

    def test_web_list_reads_saved_files_without_source_requests(self):
        state = FixtureState(pages={"19:1": ["101"]})
        with fixture_server(state) as base:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            config = self.write_config(base, fids=[19], port=port)
            self.cli(config)
            source_counts = state.counts.copy()
            row = self.records()[0]
            with tempfile.TemporaryFile(mode="w+") as log:
                process = subprocess.Popen(
                    [sys.executable, "-B", str(SCRIPT), "--config", str(config), "--serve"],
                    cwd=self.root,
                    stdout=log,
                    stderr=log,
                    text=True,
                )
                try:
                    url = f"http://127.0.0.1:{port}"
                    deadline = time.monotonic() + 8
                    while True:
                        try:
                            with urlopen(url + "/", timeout=0.5) as response:
                                page = response.read().decode("utf-8")
                            break
                        except (URLError, OSError):
                            if time.monotonic() > deadline or process.poll() is not None:
                                log.seek(0)
                                self.fail("Web service did not start: " + log.read())
                            time.sleep(0.05)
                    self.assertIn("山间记录 101", page)
                    links = re.findall(r'href=["\']([^"\']+)["\']', page)
                    archive_links = [x for x in links if re.match(r"/(?:archive|post|read)/", x)]
                    self.assertTrue(archive_links, f"No local archive links in {links}")
                    with urlopen(url + archive_links[0], timeout=2) as response:
                        saved = response.read()
                    self.assertEqual(saved, (self.root / "output" / row["filename"]).read_bytes())
                    for unsafe in ("/../config.toml", "/data/archive.sqlite3", "/output/../../config.toml"):
                        with self.assertRaises(HTTPError) as caught:
                            urlopen(url + unsafe, timeout=2)
                        self.assertIn(caught.exception.code, {400, 403, 404})
                        caught.exception.close()
                    self.assertEqual(state.counts, source_counts)
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)


class HistoryRecoveryTests(unittest.TestCase):
    """Inject interruption at the commit boundary, without any remote source."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="discuz-history-test-")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.config = archive.Config(
            forum_key="neutral-recovery", url="https://forum.invalid/index.php",
            output_dir=self.root / "output", db_path=self.root / "data/archive.sqlite3",
        )
        self.payload = b"<!doctype html><title>Neutral first snapshot</title><p>Original content.</p>"

    def store(self):
        store = archive.Store(self.config)
        self.addCleanup(store.close)
        return store

    def test_prepared_file_recovers_without_refetching(self):
        first = self.store()
        with patch.object(first, "finish", side_effect=RuntimeError("injected interruption")):
            with self.assertRaises(RuntimeError):
                first.save("101", "首次标题", self.payload)
        pending = first.db.execute("SELECT * FROM pending").fetchone()
        self.assertIsNotNone(pending)
        self.assertIsNone(first.known("101"))
        self.assertFalse((self.config.output_dir / pending["filename"]).exists())
        self.assertEqual((self.config.output_dir / pending["tmp_name"]).read_bytes(), self.payload)
        first.close()
        second = self.store()
        with patch.object(archive.Client, "__init__", side_effect=AssertionError("Recovery must be local")):
            second.recover()
        row = second.known("101")
        self.assertIsNotNone(row)
        for key in ("title", "filename", "sha256", "size", "archived_at"):
            self.assertEqual(row[key], pending[key])
        self.assertEqual((self.config.output_dir / row["filename"]).read_bytes(), self.payload)
        self.assertEqual(second.db.execute("SELECT count(*) FROM pending").fetchone()[0], 0)
        self.assertFalse((self.config.output_dir / pending["tmp_name"]).exists())

    def test_published_file_before_database_commit_recovers_original(self):
        first = self.store()

        def publish_then_interrupt(row):
            os.link(self.config.output_dir / row["tmp_name"], self.config.output_dir / row["filename"])
            raise RuntimeError("injected post-publication interruption")

        with patch.object(first, "finish", side_effect=publish_then_interrupt):
            with self.assertRaises(RuntimeError):
                first.save("101", "首次标题", self.payload)
        pending = first.db.execute("SELECT * FROM pending").fetchone()
        original_stat = (self.config.output_dir / pending["filename"]).stat()
        first.close()
        second = self.store()
        with patch.object(archive.Client, "__init__", side_effect=AssertionError("Recovery must be local")):
            second.recover()
        row = second.known("101")
        path = self.config.output_dir / row["filename"]
        self.assertEqual(path.read_bytes(), self.payload)

        self.assertEqual(path.stat().st_ino, original_stat.st_ino, "Recovery must preserve the published file")
        self.assertEqual(row["archived_at"], pending["archived_at"])

    def test_corrupt_prepared_file_refuses_commit_and_retains_evidence(self):
        store = self.store()
        with patch.object(store, "finish", side_effect=RuntimeError("injected interruption")):
            with self.assertRaises(RuntimeError):
                store.save("101", "首次标题", self.payload)
        row = store.db.execute("SELECT * FROM pending").fetchone()
        temporary = self.config.output_dir / row["tmp_name"]
        temporary.write_bytes(b"corrupt prepared file")
        with self.assertRaises(archive.ArchiveError):
            store.recover()
        self.assertIsNone(store.known("101"))
        self.assertEqual(temporary.read_bytes(), b"corrupt prepared file")
        self.assertEqual(store.db.execute("SELECT count(*) FROM pending").fetchone()[0], 1)
        self.assertFalse((self.config.output_dir / row["filename"]).exists())

    def test_success_cannot_be_updated_or_deleted_even_by_sql(self):
        store = self.store()
        store.save("101", "首次标题", self.payload)
        original = dict(store.known("101"))
        for statement in ("UPDATE archives SET title='edited'", "DELETE FROM archives"):
            with self.assertRaises(sqlite3.IntegrityError):
                with store.db:
                    store.db.execute(statement)
        store.save("101", "后来的标题", b"later edited body")
        self.assertEqual(dict(store.known("101")), original)
        self.assertEqual((self.config.output_dir / original["filename"]).read_bytes(), self.payload)

    def test_archive_path_rejects_traversal_and_symlinks(self):
        store = self.store()
        for name in ("../config.toml", "/etc/passwd", "", ".", ".."):
            with self.assertRaises(archive.ArchiveError):
                store.path(name)
        outside = self.root / "private.txt"
        outside.write_text("private", encoding="utf-8")
        (self.config.output_dir / "linked.html").symlink_to(outside)
        with self.assertRaises(archive.ArchiveError):
            store.path("linked.html")

    def test_filename_conversion_keeps_archive_inside_output(self):
        for title in ("../../unsafe/title", "a\\b\x00c", "CON", "山" * 200, "...", 'a:b?c*"d<>|'):
            filename = archive.safe_filename(title)
            self.assertEqual(Path(filename).name, filename)
            self.assertNotRegex(filename, r'[<>:"/\\|?*\x00-\x1f\x7f]')
            self.assertLessEqual(len(filename.encode("utf-8")), 255)
            self.assertTrue(filename.endswith(".html"))

    def test_same_tid_from_distinct_forums_has_independent_history(self):
        first = self.store()
        first.save("101", "同名标题", self.payload)
        first_record = dict(first.known("101"))
        other_config = archive.Config(
            forum_key="another-neutral-forum", url="https://other.invalid/index.php",
            output_dir=self.config.output_dir, db_path=self.config.db_path,
        )
        second = archive.Store(other_config)
        self.addCleanup(second.close)
        self.assertIsNone(second.known("101"))
        second.save("101", "同名标题", b"other forum snapshot")
        other_record = dict(second.known("101"))
        self.assertNotEqual(first_record["filename"], other_record["filename"])
        self.assertEqual(dict(first.known("101")), first_record)
        self.assertEqual((self.config.output_dir / first_record["filename"]).read_bytes(), self.payload)
        self.assertEqual((self.config.output_dir / other_record["filename"]).read_bytes(), b"other forum snapshot")

    def test_empty_recreated_database_is_not_silently_reinitialized(self):
        first = self.store()
        first.save("101", "首次标题", self.payload)
        path = self.config.output_dir / first.known("101")["filename"]
        first.close()
        for file in self.config.db_path.parent.glob(self.config.db_path.name + "*"):
            file.unlink()
        with closing(sqlite3.connect(self.config.db_path)):
            pass
        with self.assertRaises(archive.ArchiveError):
            archive.Store(self.config)
        self.assertEqual(path.read_bytes(), self.payload)


class DigestFilterProofTests(unittest.TestCase):
    def test_stock_encoded_listextra_confirms_server_filter_state(self):
        document = archive.Document(
            '<input type="hidden" name="listextra" value="page%3D1%26filter%3Ddigest%26digest%3D1">'
        ).root
        self.assertTrue(archive.digest_filter_active(
            document, "https://forum.invalid/forum.php?mod=forumdisplay&fid=19&filter=digest", "19",
        ))

    def test_requested_filter_and_unselected_or_other_board_tabs_are_not_proof(self):
        for tab in (
            '<a class="xi2" href="forum.php?mod=forumdisplay&amp;fid=19&amp;filter=digest">精华</a>',
            '<a class="xi2 xw1" href="forum.php?mod=forumdisplay&amp;fid=20&amp;filter=digest">精华</a>',
        ):
            with self.subTest(tab=tab):
                document = archive.Document(tab).root
                self.assertFalse(archive.digest_filter_active(
                    document, "https://forum.invalid/forum.php?mod=forumdisplay&fid=19&filter=digest", "19",
                ))


class EncodingAndWebAccessTests(unittest.TestCase):
    """Module contracts for observed mixed-encoding ads and Web authentication."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="discuz-module-test-")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.config = archive.Config(
            forum_key="neutral-module", url="https://forum.invalid/index.php",
            output_dir=self.root / "output", db_path=self.root / "data/archive.sqlite3",
        )

    def decode_page(self, raw: bytes, charset: str = "utf-8"):
        headers = Message()
        headers["Content-Type"] = "text/html; charset=" + charset
        client = archive.Client(self.config)
        with patch.object(client, "fetch", return_value=(raw, headers, self.config.url)):
            return client.page(self.config.url)

    def test_non_content_gbk_bytes_in_utf8_page_are_discarded(self):
        message = '<div id="postmessage_1">真实 UTF-8 正文不变。</div>'.encode()
        for block in (
            b'<ScRiPt type="text/javascript">/* GBK: \xd6\xd0 */</ScRiPt>',
            b'<style>/* GBK: \xd6\xd0 */</style>',
            b'<!-- GBK legacy comment: \xd6\xd0 -->',
        ):
            with self.subTest(block=block.split(b">", 1)[0]):
                root, final = self.decode_page(b"<html><body>" + message + block + b"</body></html>")
                self.assertEqual(final, self.config.url)
                self.assertIn("真实 UTF-8 正文不变。", root.text())
                self.assertNotIn("\ufffd", root.text())
                self.assertFalse(any(node.tag in {"script", "style"} for node in root.walk()))

    def test_invalid_body_encoding_still_fails_instead_of_replacement(self):
        for raw in (
            b'<div id="postmessage_1">Invalid body: \xd6\xd0</div>',
            b'<script>\xd6\xd0</script><div id="postmessage_1">Invalid body: \xff</div>',
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(archive.ArchiveError):
                    self.decode_page(raw)

    def test_valid_gbk_body_is_decoded_without_loss(self):
        root, _ = self.decode_page('<div id="postmessage_1">完整中文正文。</div>'.encode("gbk"), "gb2312")
        self.assertIn("完整中文正文。", root.text())
        self.assertNotIn("\ufffd", root.text())

    def test_unknown_charset_fails_explicitly(self):
        with self.assertRaises(archive.ArchiveError):
            self.decode_page(b"<html><p>Neutral text</p></html>", "not-a-real-charset")

    def test_remote_web_binding_requires_token_before_socket_creation(self):
        self.config.web_host = "0.0.0.0"
        with patch.object(archive.ThreadingHTTPServer, "__init__", side_effect=AssertionError("must not bind")):
            with self.assertRaises(archive.ArchiveError):
                archive.make_web_server(self.config)

    def test_short_web_token_is_rejected_before_socket_creation(self):
        token_file = self.root / "test-token"
        token_file.write_text("too-short", encoding="utf-8")
        self.config.web_token_file = token_file
        with patch.object(archive.ThreadingHTTPServer, "__init__", side_effect=AssertionError("must not bind")):
            with self.assertRaises(archive.ArchiveError):
                archive.make_web_server(self.config)

    def test_web_basic_auth_protects_list_and_diagnostics(self):
        token = secrets.token_urlsafe(24)
        token_file = self.root / "test-token"
        token_file.write_text(token, encoding="utf-8")
        self.config.web_token_file = token_file
        self.config.web_port = 0
        with closing(archive.Store(self.config)):
            pass
        server = archive.make_web_server(self.config)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with patch.object(archive.Client, "__init__", side_effect=AssertionError("Web must be local-only")):
                for path in ("/", "/api/debug/state"):
                    for authorization in (None, "Basic " + base64.b64encode(b"archive:wrong-password").decode()):
                        request = Request(base + path)
                        if authorization:
                            request.add_header("Authorization", authorization)
                        with self.assertRaises(HTTPError) as caught:
                            urlopen(request, timeout=2)
                        self.assertEqual(caught.exception.code, 401)
                        self.assertIn("Basic", caught.exception.headers["WWW-Authenticate"])
                        caught.exception.close()
                    request = Request(base + path, headers={
                        "Authorization": "Basic " + base64.b64encode(("archive:" + token).encode()).decode(),
                    })
                    with urlopen(request, timeout=2) as response:
                        payload = response.read()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertNotIn(token.encode(), payload)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
