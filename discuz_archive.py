#!/usr/bin/env python3
"""Discuz first-copy archiver and local reader. Python 3.11+, standard library only."""
from __future__ import annotations

import argparse
import base64
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import html
from html.parser import HTMLParser
from http.cookiejar import MozillaCookieJar, CookieJar
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import tomllib
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit, quote
from urllib.request import Request, build_opener, HTTPCookieProcessor, HTTPRedirectHandler
from urllib.robotparser import RobotFileParser

VERSION = '1.2.1'
LOG = logging.getLogger('discuz-archive')
TRACE = secrets.token_hex(6)
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}


class ArchiveError(RuntimeError):
    pass


class FetchError(ArchiveError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f'HTTP request failed: status={status}')


class InsufficientImages(ArchiveError):
    def __init__(self, available: int, required: int, reason: str = 'below_min_images'):
        self.available, self.required, self.reason = available, required, reason
        super().__init__(f'Available images {available} below threshold {required}: {reason}')


class JsonLog(logging.Formatter):
    def format(self, record):
        return json.dumps({'time': datetime.now(timezone.utc).isoformat(),
                           'level': record.levelname, 'trace': TRACE,
                           'event': record.getMessage()}, ensure_ascii=False)


class Progress:
    def __init__(self, config):
        self.path = config.db_path.parent / 'progress.json'
        self.state = dict(status='running', pid=os.getpid(), trace=TRACE,
                          started_at=datetime.now(timezone.utc).isoformat())
        self.lock = threading.Lock()

    def update(self, **fields):
        with self.lock:
            self._update(**fields)

    def _update(self, **fields):
        allowed = {'status', 'phase', 'fid', 'tid', 'image_attempts', 'image_available',
                   'image_failed', 'stats', 'request_kind', 'request_attempt', 'wait_seconds',
                   'download_total', 'download_completed', 'download_failed'}
        self.state.update({k: v for k, v in fields.items() if k in allowed})
        self.state['updated_at'] = datetime.now(timezone.utc).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix='.progress-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as file:
                json.dump(self.state, file)
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def read_progress(config):
    try:
        state = json.loads((config.db_path.parent / 'progress.json').read_text('utf-8'))
        if not isinstance(state, dict):
            return None
        pid = state.get('pid')
        if type(pid) is not int or pid <= 0:
            return None
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
        state['process_alive'] = alive
        if not alive and state.get('status') == 'running':
            state['status'] = 'interrupted'
        return state
    except (OSError, ValueError, TypeError):
        return None


def http_url(url: str) -> str:
    p = urlsplit(html.unescape(url.strip()))
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ArchiveError('Only HTTP(S) URLs without embedded credentials are accepted')
    return urlunsplit((p.scheme, p.netloc, p.path or '/', p.query, ''))


@dataclass
class Config:
    forum_key: str
    url: str
    output_dir: Path
    db_path: Path
    fids: list[str] = field(default_factory=list)
    exclude_fids: list[str] = field(default_factory=list)
    style: str = 'auto'
    cookie_file: Path | None = None
    drop_selectors: list[str] = field(default_factory=list)
    ad_hosts: list[str] = field(default_factory=list)
    interval: float = 2.0
    timeout: float = 25.0
    retries: int = 2
    max_pages: int = 10000
    max_image_bytes: int = 32 * 1024 * 1024
    max_post_bytes: int = 256 * 1024 * 1024
    min_images: int = 0
    image_workers: int = 4
    image_interval: float = 0.25
    respect_robots: bool = True
    web_enabled: bool = True
    web_host: str = '127.0.0.1'
    web_port: int = 8080
    web_token_file: Path | None = None
    schedule: str = '*-*-* 03:00:00'
    log_level: str = 'INFO'

    @classmethod
    def from_file(cls, path: Path) -> Config:
        path = path.resolve()
        with path.open('rb') as file:
            data = tomllib.load(file)
        forum, archive = data.get('forum', {}), data.get('archive', {})
        request, web = data.get('request', {}), data.get('web', {})

        def local(value):
            p = Path(value).expanduser()
            return (path.parent / p).resolve() if not p.is_absolute() else p.resolve()

        key = str(forum.get('key', '')).strip()
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', key):
            raise ArchiveError('forum.key must be a stable 1–80 character identifier')
        minimum = archive.get('min_images', 0)
        if type(minimum) is not int or minimum < 0:
            raise ArchiveError('archive.min_images must be a nonnegative integer')
        config = cls(
            forum_key=key, url=http_url(str(forum.get('url', ''))),
            output_dir=local(archive.get('output_dir', 'output')),
            db_path=local(archive.get('database', 'data/archive.sqlite3')),
            fids=[str(x) for x in forum.get('fids', [])],
            exclude_fids=[str(x) for x in forum.get('exclude_fids', [])],
            style=forum.get('style', 'auto'),
            cookie_file=local(forum['cookie_file']) if forum.get('cookie_file') else None,
            drop_selectors=list(forum.get('drop_selectors', [])),
            ad_hosts=list(forum.get('ad_hosts', [])),
            interval=float(request.get('interval', 2)), timeout=float(request.get('timeout', 25)),
            retries=int(request.get('retries', 2)), max_pages=int(request.get('max_pages', 10000)),
            max_image_bytes=int(request.get('max_image_mb', 32)) * 1024 * 1024,
            max_post_bytes=int(request.get('max_post_mb', 256)) * 1024 * 1024,
            min_images=minimum,
            image_workers=request.get('image_workers', 4),
            image_interval=float(request.get('image_interval', 0 if request.get('interval') == 0 else 0.25)),
            respect_robots=bool(request.get('respect_robots', True)),
            web_enabled=bool(web.get('enabled', True)),
            web_host=str(web.get('host', '127.0.0.1')), web_port=int(web.get('port', 8080)),
            web_token_file=local(web['token_file']) if web.get('token_file') else None,
            schedule=str(data.get('schedule', {}).get('on_calendar', '*-*-* 03:00:00')),
            log_level=str(data.get('logging', {}).get('level', 'INFO')).upper())
        if config.style not in {'auto', 'classic', 'x'}:
            raise ArchiveError('forum.style must be auto, classic, or x')
        if type(config.image_workers) is not int or not 1 <= config.image_workers <= 8:
            raise ArchiveError('request.image_workers must be an integer from 1 to 8')
        if not math.isfinite(config.image_interval) or config.image_interval < 0:
            raise ArchiveError('request.image_interval must be finite and nonnegative')
        if any(not x.isdecimal() or int(x) < 1 for x in config.fids + config.exclude_fids):
            raise ArchiveError('Board IDs must be positive integers')
        if not (math.isfinite(config.interval) and math.isfinite(config.timeout)
                and config.interval >= 0 and 0 < config.timeout <= 120
                and 0 <= config.retries <= 5 and config.max_pages > 0
                and config.max_image_bytes > 0 and config.max_post_bytes > 0):
            raise ArchiveError('Invalid request limits')
        if not 0 < config.web_port < 65536:
            raise ArchiveError('web.port must be between 1 and 65535')
        if config.log_level not in {'DEBUG', 'INFO', 'WARN', 'WARNING', 'ERROR'}:
            raise ArchiveError('Unsupported logging level')
        if config.output_dir == config.db_path.parent:
            raise ArchiveError('Use separate output and database directories')
        for selector in config.drop_selectors:
            if not re.fullmatch(r'[.#]?[A-Za-z_][\w-]*', selector):
                raise ArchiveError('drop_selectors accepts simple #id, .class, or tag selectors')
        return config


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list = field(default_factory=list)
    parent: Node | None = field(default=None, repr=False)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self):
        if self.tag in {'script', 'style'}:
            return ''
        return ''.join(x.text() if isinstance(x, Node) else x for x in self.children)

    def classes(self):
        return set(self.attrs.get('class', '').split())

    def ancestors(self):
        node = self.parent
        while node:
            yield node
            node = node.parent


class Document(HTMLParser):
    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.root = Node('root')
        self.stack = [self.root]
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        if len(self.stack) > 160:
            raise ArchiveError('HTML nesting limit exceeded')
        if tag in {'p', 'li'} and self.stack[-1].tag == tag:
            self.stack.pop()
        node = Node(tag, dict((k, v or '') for k, v in attrs), parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, http_url(newurl))


class Client:
    agent = f'DiscuzArchive/{VERSION}'

    def __init__(self, config: Config):
        self.config = config
        jar = CookieJar()
        if config.cookie_file:
            jar = MozillaCookieJar(str(config.cookie_file))
            jar.load(ignore_discard=True)
        self.opener = build_opener(SafeRedirect(), HTTPCookieProcessor(jar))
        self.jar = jar
        self.owner_thread = threading.get_ident()
        self.thread_local = threading.local()
        self.rate_lock = threading.Lock()
        self.robots_lock = threading.Lock()
        self.last_request = 0.0
        self.robots = {}
        self.progress = None
        self.request_kind = 'page'

    def _get(self, url: str, limit: int, referer: str = ''):
        url = http_url(url)
        headers = {'User-Agent': self.agent, 'Accept-Encoding': 'identity'}
        if referer:
            headers['Referer'] = referer
        for attempt in range(self.config.retries + 1):
            kind = 'robots' if urlsplit(url).path.endswith('/robots.txt') else self.request_kind
            interval = self.config.image_interval if kind == 'image' else self.config.interval
            with self.rate_lock:
                wait = interval - (time.monotonic() - self.last_request)
                if wait > 0:
                    time.sleep(wait)
                self.last_request = time.monotonic()
            if self.progress:
                self.progress.update(request_kind=kind, request_attempt=attempt+1, wait_seconds=round(max(0, wait), 2))
            LOG.debug('http_start kind=%s attempt=%d wait_seconds=%.2f', kind, attempt+1, max(0, wait))
            started = time.monotonic()
            try:
                opener = self.opener
                if threading.get_ident() != self.owner_thread:
                    if not hasattr(self.thread_local, 'opener'):
                        self.thread_local.opener = build_opener(SafeRedirect(), HTTPCookieProcessor(self.jar))
                    opener = self.thread_local.opener
                with opener.open(Request(url, headers=headers), timeout=self.config.timeout) as response:
                    if response.headers.get('Content-Encoding', 'identity').lower() not in ('identity', ''):
                        raise ArchiveError('Unexpected compressed response')
                    body = response.read(limit + 1)
                    if len(body) > limit:
                        raise ArchiveError('Response exceeds configured size limit')
                    declared_length = response.headers.get('Content-Length', '')
                    if declared_length.isdecimal() and int(declared_length) != len(body):
                        raise ArchiveError('Incomplete HTTP response body')
                    LOG.debug('http kind=%s status=%d bytes=%d elapsed_ms=%d', kind, response.status, len(body),
                              int((time.monotonic()-started)*1000))
                    return body, response.headers, http_url(response.url)
            except HTTPError as error:
                status = error.code
                LOG.debug('http_error kind=%s status=%d attempt=%d', kind, status, attempt+1)
                retry_after = error.headers.get('Retry-After', '')
                error.close()
                if status not in {429, 500, 502, 503, 504} or attempt == self.config.retries:
                    raise FetchError(status) from None
                if retry_after.isdecimal() and int(retry_after) > 30:
                    raise ArchiveError('Server requested a longer pause; retry on a later run') from None
                time.sleep(max(2 ** attempt, int(retry_after) if retry_after.isdecimal() else 0))
            except (URLError, TimeoutError, OSError, HTTPException):
                LOG.debug('http_network_error kind=%s attempt=%d', kind, attempt+1)
                if attempt == self.config.retries:
                    raise ArchiveError('Network request failed or timed out') from None
                time.sleep(2 ** attempt)
        raise ArchiveError('Request failed')

    def fetch(self, url: str, limit: int, referer: str = ''):
        url = http_url(url)
        origin = urlunsplit((*urlsplit(url)[:2], '', '', ''))
        if self.config.respect_robots:
            with self.robots_lock:
                self._check_robots(origin, url)
        return self._get(url, limit, referer)

    def _check_robots(self, origin, url):
        if origin not in self.robots:
            parser = RobotFileParser()
            try:
                body, _, _ = self._get(origin + '/robots.txt', 512 * 1024)
                parser.parse(body.decode('utf-8', errors='replace').splitlines())
            except ArchiveError as error:
                if 'status=404' in str(error) or 'status=410' in str(error):
                    parser.parse(['User-agent: *', 'Allow: /'])
                elif 'status=401' in str(error) or 'status=403' in str(error):
                    parser.parse(['User-agent: *', 'Disallow: /'])
                else:
                    raise ArchiveError('Cannot verify robots.txt; request skipped') from None
            self.robots[origin] = parser
        if not self.robots[origin].can_fetch(self.agent, url):
            raise ArchiveError('robots.txt disallows this request')

    def page(self, url: str, referer: str = ''):
        raw, headers, final = self.fetch(url, 8 * 1024 * 1024, referer)
        # A mirror redirect must be explicitly configured rather than silently changing identities.
        if urlsplit(final).hostname != urlsplit(self.config.url).hostname:
            raise ArchiveError('Forum redirected to a different host; update forum.url explicitly')
        encoding = headers.get_content_charset()
        if not encoding:
            match = re.search(rb'charset\s*=\s*["\x27]?\s*([\w-]+)', raw[:8192], re.I)
            encoding = match.group(1).decode('ascii') if match else 'utf-8'
        if encoding.lower() in {'gb2312', 'gbk', 'gb18030'}:
            encoding = 'gb18030'
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            # Some UTF-8 forums embed GBK comments in legacy ad scripts. These
            # executable/non-content blocks are discarded anyway; never replace
            # undecodable bytes in the actual post with lossy placeholder text.
            cleaned = re.sub(rb'<(script|style)\b[^>]*>.*?</\1\s*>', b'', raw, flags=re.I | re.S)
            cleaned = re.sub(rb'<!--.*?-->', b'', cleaned, flags=re.S)
            try:
                text = cleaned.decode(encoding)
            except (UnicodeError, LookupError):
                raise ArchiveError('Unsupported or invalid forum content encoding') from None
            LOG.debug('discarded non-content blocks with invalid text encoding')
        except LookupError:
            raise ArchiveError('Unsupported or invalid forum text encoding') from None
        return Document(text).root, final


def link_identity(url: str):
    p = urlsplit(html.unescape(url))
    q = parse_qs(p.query)
    if p.path.endswith('forumdisplay.php') or q.get('mod') == ['forumdisplay']:
        value = q.get('fid', [''])[0]
        return ('board', value) if value.isdecimal() else None
    if p.path.endswith('viewthread.php') or q.get('mod') == ['viewthread']:
        value = q.get('tid', [''])[0]
        return ('thread', value) if value.isdecimal() else None
    match = re.search(r'/(forum|thread)-(\d+)-\d+(?:-\d+)?\.html$', p.path)
    if match:
        return ('board' if match[1] == 'forum' else 'thread', match[2])
    return None


def forum_style(root: Node, url: str, configured: str):
    if configured != 'auto':
        return configured
    if urlsplit(url).path.endswith('forum.php'):
        return 'x'
    for node in root.walk():
        href = node.attrs.get('href', '')
        if 'forumdisplay.php' in href:
            return 'classic'
        if 'mod=forumdisplay' in href or re.search(r'forum-\d+-\d+\.html', href):
            return 'x'
    return 'classic'


def board_ids(root: Node, base: str):
    result = []
    for node in root.walk():
        if node.tag != 'a':
            continue
        url = urljoin(base, node.attrs.get('href', ''))
        identity = link_identity(url)
        if identity and identity[0] == 'board' and urlsplit(url).netloc == urlsplit(base).netloc:
            if identity[1] not in result:
                result.append(identity[1])
    return result


def board_url(base: str, style: str, fid: str, page: int, *, digest=True):
    params = {'fid': fid, 'page': str(page)}
    if digest:
        params['filter'] = 'digest'
    if style == 'x':
        params['mod'] = 'forumdisplay'
        if digest:
            params['digest'] = '1'
    return urljoin(base, ('forum.php' if style == 'x' else 'forumdisplay.php') + '?' + urlencode(params))


def thread_url(base: str, style: str, tid: str):
    params = {'tid': tid, 'page': '1', 'ordertype': '2'}
    if style == 'x':
        params['mod'] = 'viewthread'
    return urljoin(base, ('forum.php' if style == 'x' else 'viewthread.php') + '?' + urlencode(params))


def digest_filter_active(root: Node, base: str, fid: str):
    """Confirm server-rendered filter state; a requested URL alone is not proof."""
    for node in root.walk():
        if node.tag == 'a':
            url = urljoin(base, node.attrs.get('href', ''))
            if (link_identity(url) == ('board', fid) and
                    urlsplit(url).netloc == urlsplit(base).netloc and
                    parse_qs(urlsplit(url).query).get('filter') == ['digest'] and
                    node.classes() & {'xw1', 'current', 'active', 'a'}):
                return True
        if node.tag == 'input' and node.attrs.get('name') == 'listextra':
            value = node.attrs.get('value', '')
            # listextra is URL-encoded by stock templates; decoding it through
            # parse_qs also preserves query separators contained in its value.
            from urllib.parse import unquote
            if parse_qs(unquote(value)).get('filter') == ['digest']:
                return True
    return False


def sticky_ids(root: Node):
    return {match[1] for node in root.walk()
            if (match := re.fullmatch(r'stickthread_(\d+)', node.attrs.get('id', '')))}


def digest_threads(root: Node, base: str, digest_confirmed=False, verified_stickies=None):
    verified_stickies = verified_stickies or set()
    found = set()
    for link in root.walk():
        if link.tag != 'a':
            continue
        url = urljoin(base, link.attrs.get('href', ''))
        identity = link_identity(url)
        if not identity or identity[0] != 'thread' or urlsplit(url).netloc != urlsplit(base).netloc:
            continue
        row = None
        for ancestor in link.ancestors():
            if re.match(r'(?:normal|stick)thread_\d+', ancestor.attrs.get('id', '')):
                row = ancestor
                break
            if ancestor.tag in {'tr', 'li'} and row is None:
                row = ancestor
        if row is None:
            continue
        marked = any(
            ('digest' in node.classes()) or
            (node.tag == 'img' and (re.search(r'(?:^|/)digest(?:[_-][123])?\.', node.attrs.get('src', ''), re.I)
                                   or '精华' in node.attrs.get('alt', '') or '精华' in node.attrs.get('title', '')))
            for node in row.walk())
        # Stock Discuz deliberately suppresses digest icons while this filter
        # is active. Accept ordinary rows only after confirming the response's
        # active filter; unmarked sticky rows need a separate ordinary-list check.
        ordinary = bool(re.fullmatch(r'normalthread_\d+', row.attrs.get('id', '')))
        if row.tag == 'li' and any(n.attrs.get('id') == 'waterfall' for n in row.ancestors()):
            ordinary = True
        accepted = marked or (digest_confirmed and ordinary) or identity[1] in verified_stickies
        if accepted and identity[1] not in found:
            found.add(identity[1])
            yield identity[1]


def last_page(root: Node, fid: str, base: str, current: int):
    maximum = current
    for node in root.walk():
        if node.tag != 'a':
            continue
        url = urljoin(base, node.attrs.get('href', ''))
        identity = link_identity(url)
        if identity != ('board', fid) or urlsplit(url).netloc != urlsplit(base).netloc:
            continue
        q = parse_qs(urlsplit(url).query)
        page = q.get('page', [''])[0]
        if not page:
            m = re.search(r'/forum-\d+-(\d+)\.html$', urlsplit(url).path)
            page = m[1] if m else ''
        if page.isdecimal():
            maximum = max(maximum, int(page))
    return maximum


DROP_TAGS = {'script', 'style', 'iframe', 'object', 'embed', 'form', 'input', 'button',
             'textarea', 'select', 'option', 'link', 'meta', 'base', 'svg', 'canvas',
             'video', 'audio', 'source', 'noscript'}
SAFE_TAGS = {'p', 'br', 'div', 'span', 'strong', 'b', 'em', 'i', 'u', 's', 'del', 'sup',
             'sub', 'ul', 'ol', 'li', 'blockquote', 'pre', 'code', 'h2', 'h3', 'h4',
             'h5', 'h6', 'hr', 'table', 'thead', 'tbody', 'tr', 'td', 'th', 'a', 'img'}
UI_CLASSES = {'sign', 'signatures', 'signature', 'pob', 'postactions', 'a_pr', 'a_pt',
              'a_pb', 'a_p', 'ad', 'ads', 'advertisement', 'banner', 'share', 'favorite',
              'recommend', 'pstatus', 't_attach', 'tip', 'rate', 'ratl', 'attachinfo'}
UI_ID = re.compile(r'^(?:ad(?:_|-)|adv_|recommend_|favorite|share|rate_|ratelog|postaction)', re.I)


def discard(node: Node, config: Config):
    attrs = node.attrs
    if node.tag in DROP_TAGS or node.classes() & UI_CLASSES or UI_ID.match(attrs.get('id', '')):
        return True
    if attrs.get('hidden') is not None or re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', attrs.get('style', ''), re.I):
        return True
    if re.match(r'aimg_.*_menu$', attrs.get('id', '')):
        return True
    for selector in config.drop_selectors:
        if (selector.startswith('#') and attrs.get('id') == selector[1:]
                or selector.startswith('.') and selector[1:] in node.classes()
                or selector == node.tag):
            return True
    return False


def image_type(data: bytes):
    if data[:6] in (b'GIF87a', b'GIF89a') and len(data) > 13 and data.endswith(b';'):
        return 'image/gif'
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) > 32 and b'IEND' in data[-16:]:
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff') and data.endswith(b'\xff\xd9'):
        return 'image/jpeg'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP' and len(data) >= 20:
        return 'image/webp'
    if len(data) > 24 and data[4:8] == b'ftyp' and data[8:12] in {b'avif', b'avis'}:
        return 'image/avif'
    raise ArchiveError('Image is incomplete or has an unsupported/invalid format')


class ImageWindow:
    """At most N queued/results at once; preserve article order without bulk buffering."""
    def __init__(self, client, page, urls):
        self.client, self.page = client, page
        self.remaining = deque(urls)
        self.pending = {}
        self.total, self.completed, self.failed = len(urls), 0, 0
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=client.config.image_workers,
                                       thread_name_prefix='discuz-image')
        client.request_kind = 'image'
        if client.progress:
            client.progress.update(phase='images', download_total=self.total,
                                   download_completed=0, download_failed=0)
        LOG.info('image_batch_started total=%d workers=%d', self.total, client.config.image_workers)
        for _ in range(client.config.image_workers):
            self.submit_next()

    def submit_next(self):
        url = self.remaining.popleft() if self.remaining else None
        if url is not None:
            self.pending[url] = self.pool.submit(self.download, url)

    def download(self, url):
        failed = False
        try:
            result = self.client.fetch(url, self.client.config.max_image_bytes, self.page)
            image_type(result[0])
            return result
        except BaseException:
            failed = True
            raise
        finally:
            with self.lock:
                self.completed += 1
                self.failed += int(failed)
                LOG.info('image_download_progress completed=%d total=%d failed=%d',
                         self.completed, self.total, self.failed)
                if self.client.progress:
                    self.client.progress.update(download_completed=self.completed, download_failed=self.failed)

    def get(self, url):
        future = self.pending.pop(url, None)
        if future is None:
            if url in self.remaining:
                self.remaining.remove(url)
            return self.pool.submit(self.download, url).result()
        try:
            return future.result()
        finally:
            self.submit_next()

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)


class Cleaner:
    def __init__(self, client: Client, page: str):
        self.client, self.config, self.page = client, client.config, page
        self.images = {}
        self.failed_sources = set()
        self.total_bytes = 0
        self.attempts = 0
        self.window = None

    def prepare(self, body, container):
        if self.config.image_workers == 1:
            return
        included = {id(n) for n in body.walk()}
        urls = []
        nodes = list(body.walk()) + [n for n in container.walk() if id(n) not in included]
        for node in nodes:
            if node.tag != 'img' or not (id(node) in included or node.attrs.get('id', '').startswith('aimg_') or node.attrs.get('aid')):
                continue
            scope = [node]
            for ancestor in node.ancestors():
                scope.append(ancestor)
                if ancestor is container:
                    break
            if any(discard(n, self.config) or needs_access(n) for n in scope):
                continue
            a = node.attrs
            source = next((a.get(k) for k in ('zoomfile','file','data-original','data-src','src') if a.get(k)), '')
            if (not source or source.startswith('data:') or '/smiley/' in source or '/smilies/' in source
                    or a.get('width') == '1' and a.get('height') == '1'):
                continue
            try:
                url = http_url(urljoin(self.page, source))
                if re.search(r'(?:^|/)(?:static/image/common|images/default)/(?:none|loading|imgloading|placeholder)\.(?:gif|png)$', urlsplit(url).path, re.I):
                    continue
                host = urlsplit(url).hostname or ''
                if any(host == h or host.endswith('.'+h) for h in self.config.ad_hosts):
                    continue
                if url not in urls:
                    urls.append(url)
            except (ArchiveError, ValueError):
                continue
        if urls:
            self.window = ImageWindow(self.client, self.page, urls[:1000])

    def close(self):
        if self.window:
            self.window.close()

    def image(self, node: Node):
        # Failed sources are cached too: repeated broken tags must not multiply
        # network attempts or count as additional images.
        source = next((node.attrs.get(key) for key in
                       ('zoomfile', 'file', 'data-original', 'data-src', 'src') if node.attrs.get(key)), '')
        attempt_key = source
        self.attempts += 1
        identity = link_identity(self.page)
        tid = identity[1] if identity else 'unknown'
        progress = getattr(self.client, 'progress', None)
        self.client.request_kind = 'image'
        LOG.info('image_started tid=%s index=%d', tid, self.attempts)
        if progress:
            progress.update(phase='images', image_attempts=self.attempts)
        try:
            if not source.startswith('data:'):
                attempt_key = http_url(urljoin(self.page, source))
            if self.config.min_images and attempt_key in self.failed_sources:
                return ''
            return self._image(node)
        except (ArchiveError, ValueError) as error:
            self.failed_sources.add(attempt_key)
            if not self.config.min_images:
                raise
            identity = link_identity(self.page)
            LOG.warning('image_unavailable tid=%s status=%s reason_type=%s available=%d',
                        identity[1] if identity else 'unknown', getattr(error, 'status', None),
                        type(error).__name__, len(self.images))
            return ''
        finally:
            LOG.info('image_progress tid=%s processed=%d available=%d failed=%d bytes=%d',
                     tid, self.attempts, len(self.images), len(self.failed_sources), self.total_bytes)
            if progress:
                progress.update(image_attempts=self.attempts, image_available=len(self.images),
                                image_failed=len(self.failed_sources))

    def _image(self, node: Node):
        a = node.attrs
        source = next((a.get(key) for key in ('zoomfile', 'file', 'data-original', 'data-src', 'src') if a.get(key)), '')
        if not source:
            raise ArchiveError('An inline image has no usable source')
        if ('/smiley/' in source or '/smilies/' in source):
            return html.escape(a.get('alt', ''))
        if re.search(r'(?:^|/)(?:static/image/common|images/default)/(?:none|loading|imgloading|placeholder)\.(?:gif|png)$',
                     urlsplit(source).path, re.I):
            return ''
        if a.get('width') == '1' and a.get('height') == '1':
            return ''
        if source.startswith('data:'):
            match = re.fullmatch(r'data:image/[\w.+-]+;base64,([A-Za-z0-9+/=\s]+)', source)
            if not match:
                raise ArchiveError('Unsupported inline image data')
            data = base64.b64decode(re.sub(r'\s+', '', match[1]), validate=True)
            if len(data) > self.config.max_image_bytes:
                raise ArchiveError('Inline image exceeds size limit')
            key = hashlib.sha256(data).hexdigest()
        else:
            source = http_url(urljoin(self.page, source))
            host = urlsplit(source).hostname or ''
            if any(host == h or host.endswith('.'+h) for h in self.config.ad_hosts):
                return ''
            key = source
            data = None
        if key not in self.images:
            if len(self.images) >= 1000:
                raise ArchiveError('Image count limit exceeded')
            if data is None:
                data, _, _ = (self.window.get(source) if self.window else
                              self.client.fetch(source, self.config.max_image_bytes, self.page))
            mime = image_type(data)
            if (self.total_bytes + len(data)) * 4 // 3 > self.config.max_post_bytes:
                raise ArchiveError('Post exceeds configured archive size limit')
            self.total_bytes += len(data)
            self.images[key] = 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii')
        return '<img loading="lazy" src="' + self.images[key] + '" alt="' + html.escape(a.get('alt', ''), quote=True) + '">'

    def render(self, node):
        if not isinstance(node, Node):
            return html.escape(node)
        if discard(node, self.config):
            return ''
        if needs_access(node):
            if self.config.min_images:
                return ''
            raise ArchiveError('Main post contains content requiring additional access')
        if node.tag == 'img':
            return self.image(node)
        content = ''.join(self.render(child) for child in node.children)
        if node.tag not in SAFE_TAGS:
            return content
        attrs = ''
        if node.tag == 'a':
            if not content.strip():
                return ''
            href = urljoin(self.page, node.attrs.get('href', ''))
            p = urlsplit(href)
            if p.scheme not in ('http', 'https') or p.username or p.password:
                return content
            if re.search(r'(?:mod=spacecp|action=(?:recommend|rate)|ac=favorite)', p.query):
                return ''
            attrs = ' href="' + html.escape(href, quote=True) + '" rel="noreferrer noopener" target="_blank"'
        if node.tag in {'td', 'th'}:
            for key in ('colspan', 'rowspan'):
                value = node.attrs.get(key, '')
                if value.isdecimal() and 0 < int(value) <= 100:
                    attrs += f' {key}="{value}"'
        if node.tag in VOID:
            return f'<{node.tag}{attrs}>'
        return f'<{node.tag}{attrs}>{content}</{node.tag}>'


STYLE = '''
:root{color-scheme:light;--ink:#29342e;--muted:#7c857f;--line:#e6e9e5}*{box-sizing:border-box}
html{background:#f7f8f5}body{margin:0;color:var(--ink);font:16px/1.9 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:900px;margin:48px auto;background:#fff;padding:48px 64px;border:1px solid var(--line);border-radius:3px}
h1{font-size:30px;line-height:1.5;margin:0 0 16px;overflow-wrap:anywhere}h2{font-size:22px}a{color:#39634d;text-underline-offset:4px}
.meta,.muted,footer{color:var(--muted);font-size:13px}.meta{padding-bottom:24px;border-bottom:1px solid var(--line);margin-bottom:28px}
p{margin:0 0 20px}article{overflow-wrap:anywhere}article img{display:block;max-width:100%;height:auto;margin:20px auto}
blockquote{margin:24px 0;padding:8px 20px;border-left:3px solid #8ca794;background:#f7f9f6}blockquote p{margin:0}
pre{overflow:auto;padding:16px;background:#f6f8f5}table{border-collapse:collapse;max-width:100%;display:block;overflow:auto}td,th{padding:8px;border:1px solid var(--line)}
.posts{list-style:none;padding:0;margin:28px 0}.posts li{border-top:1px solid var(--line)}.posts a{display:flex;justify-content:space-between;gap:20px;padding:18px 0;text-decoration:none}.posts a:hover{color:#172e20}.posts time{color:var(--muted);font-size:12px;white-space:nowrap}
nav{display:flex;gap:24px;margin-top:24px;font-size:14px}footer{margin-top:36px;padding-top:18px;border-top:1px solid var(--line)}summary{cursor:pointer}details p{margin:12px 0 0}.label{font-size:12px;color:#527461;letter-spacing:.1em}
@media(max-width:650px){html{background:#fff}main{margin:0;padding:28px 22px;border:0}h1{font-size:25px}.posts a{display:block}.posts time{display:block;margin-top:4px}}
@media print{html{background:#fff}main{border:0;margin:0;padding:0;max-width:none}footer{display:none}}
'''
CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"


def shell(title: str, content: str, *, web=False):
    extras = ('<link rel="manifest" href="/manifest.webmanifest"><link rel="icon" href="/icon.svg">'
              '<meta name="theme-color" content="#39634d"><script src="/app.js" defer></script>') if web else ''
    policy = CSP + ("; script-src 'self'; worker-src 'self'; manifest-src 'self'; connect-src 'self'; img-src-elem 'self' data:" if web else '')
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="' + html.escape(policy, quote=True) + '">'
            '<title>' + html.escape(title) + '</title>' + extras + '<style>' + STYLE + '</style>'
            '</head><body><main>' + content + '</main></body></html>').encode('utf-8')


def needs_access(node: Node):
    return bool(node.classes() & {'attach_nopermission', 'locked', 'showhide'} or
                node.attrs.get('id', '').startswith('attach_nopermission'))


def extract_post(root: Node, client: Client, page: str):
    nodes = list(root.walk())
    bodies = [n for n in nodes if re.fullmatch(r'postmessage_\d+', n.attrs.get('id', ''))]
    if not bodies:
        if client.config.min_images and re.search(
                r'请先登录|需要登录|无权阅读|无权访问|没有权限|阅读权限不足|permission denied|access denied|login required',
                root.text(), re.I):
            raise InsufficientImages(0, client.config.min_images, 'access_restricted')
        raise ArchiveError('No main post found; unsupported template, login, or access restriction')
    body = bodies[0]
    pid = body.attrs['id'].split('_')[-1]
    ancestors = list(body.ancestors())
    container = next((n for n in ancestors if n.attrs.get('id') == 'post_'+pid), None)
    if container is None:
        container = next((n for n in ancestors if n.attrs.get('id') == 'pid'+pid), body)
    restricted = any(needs_access(n) for n in container.walk())
    if restricted and not client.config.min_images:
        raise ArchiveError('Main post or attachments require additional access; not a complete archive')
    heading = next((n for n in nodes if n.attrs.get('id') == 'thread_subject'), None)
    if heading is None:
        heading = next((n for n in nodes if n.tag == 'h1'), None)
    if heading is None or not heading.text().strip():
        raise ArchiveError('Thread title not found')
    title = heading.text().strip()
    if len(title) > 2000:
        raise ArchiveError('Thread title is too long')
    # Reject explicitly numbered reply floors even if a server ignored ascending/page=1.
    for node in container.walk():
        if node.tag == 'strong':
            m = re.fullmatch(r'\s*(\d+)\s*#\s*', node.text())
            if m and int(m[1]) != 1:
                raise ArchiveError('Server returned a reply instead of the first floor')
    author_node = next((n for n in container.walk() if n.tag == 'a' and
                        ('uid=' in n.attrs.get('href', '') or 'xw1' in n.classes())), None)
    author = author_node.text().strip() if author_node else ''
    published = next((n.text().strip() for n in container.walk()
                      if n.attrs.get('id', '').startswith('authorposton')), '')
    cleaner = Cleaner(client, page)
    body_scope = [body]
    if container is not body:
        for ancestor in ancestors:
            body_scope.append(ancestor)
            if ancestor is container:
                break
    protected_body = any(needs_access(n) for n in body_scope)
    try:
        cleaner.prepare(body, container)
        content = '' if client.config.min_images and protected_body else cleaner.render(body)
        included = {id(n) for n in body.walk()}
        # Discuz sometimes stores original image attachments beside postmessage, within the same floor.
        for node in container.walk():
            if node.tag == 'img' and id(node) not in included and (
                    node.attrs.get('id', '').startswith('aimg_') or node.attrs.get('aid')):
                if not any(discard(n, client.config) or needs_access(n) for n in [node, *node.ancestors()] if n is not container.parent):
                    content += cleaner.image(node)
    finally:
        cleaner.close()
    if len(cleaner.images) < client.config.min_images:
        reason = 'access_restricted' if restricted else 'below_min_images'
        raise InsufficientImages(len(cleaner.images), client.config.min_images, reason)
    if not re.sub(r'<[^>]+>|\s+', '', content) and not cleaner.images:
        raise ArchiveError('Main post is empty after cleaning')
    metadata = ' · '.join(html.escape(s) for s in (author, published) if s)
    about = ('<footer><details><summary>关于存档</summary><p>来源：<a rel="noreferrer" href="' +
             html.escape(page, quote=True) + '">原帖</a> · 内容版权归原作者所有。'
             '此文件保存首次接受归档时可获取的内容。作者：***。</p></details></footer>')
    result = shell(title, '<article><h1>' + html.escape(title) + '</h1><div class="meta">' +
                   metadata + '</div>' + content + '</article>' + about)
    if len(result) > client.config.max_post_bytes:
        raise ArchiveError('Final HTML exceeds configured size limit')
    return title, result


def safe_filename(title: str):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', title).strip(' .')
    value = value.encode('utf-8')[:180].decode('utf-8', errors='ignore').rstrip(' .') or '未命名帖子'
    if value.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM'+str(n) for n in range(1,10)), *('LPT'+str(n) for n in range(1,10))}:
        value = '_' + value
    return value + '.html'


def fsync_dir(path: Path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def crawl_lock(config: Config):
    config.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(str(config.db_path)+'.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ArchiveError('Another crawl is already running') from None
        yield
    finally:
        os.close(descriptor)


SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archives(
 id INTEGER PRIMARY KEY, forum_key TEXT NOT NULL, tid TEXT NOT NULL,
 title TEXT NOT NULL, filename TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL,
 size INTEGER NOT NULL, archived_at TEXT NOT NULL,
 UNIQUE(forum_key,tid));
CREATE INDEX IF NOT EXISTS archive_date ON archives(archived_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS pending(
 forum_key TEXT NOT NULL, tid TEXT NOT NULL, title TEXT NOT NULL,
 filename TEXT NOT NULL UNIQUE, tmp_name TEXT NOT NULL, sha256 TEXT NOT NULL,
 size INTEGER NOT NULL, archived_at TEXT NOT NULL, PRIMARY KEY(forum_key,tid));
CREATE TABLE IF NOT EXISTS rejected(
 forum_key TEXT NOT NULL, tid TEXT NOT NULL, available_images INTEGER NOT NULL,
 min_images INTEGER NOT NULL, reason TEXT NOT NULL, rejected_at TEXT NOT NULL,
 PRIMARY KEY(forum_key,tid), CHECK(available_images >= 0 AND min_images > available_images));
CREATE TRIGGER IF NOT EXISTS immutable_update BEFORE UPDATE ON archives
 BEGIN SELECT RAISE(ABORT,'successful archives are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_delete BEFORE DELETE ON archives
 BEGIN SELECT RAISE(ABORT,'successful archives are immutable'); END;
CREATE TRIGGER IF NOT EXISTS rejected_immutable_update BEFORE UPDATE ON rejected
 BEGIN SELECT RAISE(ABORT,'permanent rejections are immutable'); END;
CREATE TRIGGER IF NOT EXISTS rejected_immutable_delete BEFORE DELETE ON rejected
 BEGIN SELECT RAISE(ABORT,'permanent rejections are immutable'); END;
CREATE TRIGGER IF NOT EXISTS archive_excludes_rejected BEFORE INSERT ON archives
 WHEN EXISTS(SELECT 1 FROM rejected WHERE forum_key=NEW.forum_key AND tid=NEW.tid)
 BEGIN SELECT RAISE(ABORT,'post was permanently rejected'); END;
CREATE TRIGGER IF NOT EXISTS rejected_excludes_archives_pending BEFORE INSERT ON rejected
 WHEN EXISTS(SELECT 1 FROM archives WHERE forum_key=NEW.forum_key AND tid=NEW.tid)
 OR EXISTS(SELECT 1 FROM pending WHERE forum_key=NEW.forum_key AND tid=NEW.tid)
 BEGIN SELECT RAISE(ABORT,'post is already archived or prepared'); END;
CREATE TRIGGER IF NOT EXISTS pending_excludes_terminal BEFORE INSERT ON pending
 WHEN EXISTS(SELECT 1 FROM archives WHERE forum_key=NEW.forum_key AND tid=NEW.tid)
 OR EXISTS(SELECT 1 FROM rejected WHERE forum_key=NEW.forum_key AND tid=NEW.tid)
 BEGIN SELECT RAISE(ABORT,'post has terminal history'); END;
PRAGMA user_version=2;
'''


class Store:
    def __init__(self, config: Config):
        self.config = config
        output = config.output_dir
        marker = output / '.discuz-archive.json'
        existed = config.db_path.is_file()
        if not existed and (marker.exists() or any(output.glob('*.html'))):
            raise ArchiveError('History database is missing but archive files/identity exist; restore your backup')
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        config.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(config.db_path, timeout=15)
        try:
            self._initialize(existed, marker)
        except BaseException:
            self.db.close()
            raise

    def _initialize(self, existed, marker):
        config, output = self.config, self.config.output_dir
        os.chmod(config.db_path, 0o600)
        self.db.row_factory = sqlite3.Row
        if existed:
            tables = {x[0] for x in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            required = {'archives', 'pending', 'meta'} | ({'rejected'} if version == 2 else set())
            if not required <= tables or version not in {1, 2}:
                raise ArchiveError('Unknown or incomplete history schema; do not initialize over existing archives')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA + '\nCOMMIT;')
        identity = self.db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
        if identity is None:
            if marker.exists() or self.db.execute('SELECT count(*) FROM archives').fetchone()[0] or rejected_count(self.db):
                raise ArchiveError('Archive identity is missing; restore the history backup')
            identity = [secrets.token_hex(16)]
            with self.db:
                self.db.execute("INSERT INTO meta VALUES('identity',?)", (identity[0],))
        if marker.exists():
            if json.loads(marker.read_text('utf-8')).get('identity') != identity[0]:
                raise ArchiveError('Output directory belongs to a different history database')
        else:
            if self.db.execute('SELECT count(*) FROM archives').fetchone()[0] or rejected_count(self.db):
                raise ArchiveError('Output identity marker is missing; restore it from backup')
            with marker.open('x', encoding='utf-8') as file:
                json.dump({'identity': identity[0], 'version': 1}, file)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(marker, 0o600)
            fsync_dir(output)

    def close(self):
        self.db.close()

    def known(self, tid):
        return self.db.execute('SELECT * FROM archives WHERE forum_key=? AND tid=?',
                               (self.config.forum_key, tid)).fetchone()

    def rejection(self, tid):
        return self.db.execute('SELECT * FROM rejected WHERE forum_key=? AND tid=?',
                               (self.config.forum_key, tid)).fetchone()

    def terminal(self, tid):
        return bool(self.known(tid) or self.rejection(tid))

    def reject(self, tid: str, error: InsufficientImages):
        if self.terminal(tid):
            return False
        with self.db:
            cursor = self.db.execute('INSERT INTO rejected VALUES(?,?,?,?,?,?)',
                                    (self.config.forum_key, tid, error.available, error.required,
                                     error.reason, datetime.now(timezone.utc).isoformat(timespec='seconds')))
        return cursor.rowcount == 1

    def path(self, filename: str):
        if Path(filename).name != filename or not filename or filename in {'.', '..'}:
            raise ArchiveError('Invalid archive filename in history')
        path = self.config.output_dir / filename
        if path.is_symlink():
            raise ArchiveError('Archive symlinks are not accepted')
        return path

    def finish(self, row):
        target, temporary = self.path(row['filename']), self.path(row['tmp_name'])
        source = target if target.exists() else temporary
        if not source.is_file():
            raise ArchiveError('Prepared archive file is missing; restore local state before retrying')
        with source.open('rb') as file:
            digest = hashlib.file_digest(file, 'sha256').hexdigest()
        if digest != row['sha256'] or source.stat().st_size != row['size']:
            raise ArchiveError('Prepared archive checksum mismatch; refusing to overwrite or refetch')
        if not target.exists():
            os.link(temporary, target)  # Same directory/filesystem; never replace an existing file.
            fsync_dir(self.config.output_dir)
        with self.db:
            self.db.execute('INSERT INTO archives(forum_key,tid,title,filename,sha256,size,archived_at) '
                            'VALUES(?,?,?,?,?,?,?)', tuple(row[k] for k in
                             ('forum_key','tid','title','filename','sha256','size','archived_at')))
            self.db.execute('DELETE FROM pending WHERE forum_key=? AND tid=?', (row['forum_key'], row['tid']))
        temporary.unlink(missing_ok=True)
        fsync_dir(self.config.output_dir)

    def recover(self):
        for row in self.db.execute('SELECT * FROM pending').fetchall():
            self.finish(row)
            LOG.info('recovered tid=%s', row['tid'])

    def save(self, tid: str, title: str, data: bytes):
        if self.terminal(tid):
            return
        filename = safe_filename(title)
        reserved = lambda name: self.db.execute('SELECT 1 FROM archives WHERE filename=? UNION ALL '
                                                 'SELECT 1 FROM pending WHERE filename=?', (name,name)).fetchone()
        if self.path(filename).exists() or reserved(filename):
            suffix = hashlib.sha256(self.config.forum_key.encode()).hexdigest()[:8]
            filename = filename[:-5] + f' [{suffix}-{tid}].html'
        if self.path(filename).exists() or reserved(filename):
            raise ArchiveError('Archive filename collision; existing files will never be overwritten')
        descriptor, tmp = tempfile.mkstemp(prefix='.pending-', suffix='.tmp', dir=self.config.output_dir)
        try:
            with os.fdopen(descriptor, 'wb') as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            fsync_dir(self.config.output_dir)
            row = dict(forum_key=self.config.forum_key, tid=tid, title=title, filename=filename,
                       tmp_name=Path(tmp).name, sha256=hashlib.sha256(data).hexdigest(), size=len(data),
                       archived_at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
            with self.db:
                self.db.execute('INSERT INTO pending VALUES(:forum_key,:tid,:title,:filename,:tmp_name,:sha256,:size,:archived_at)', row)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        # If commit fails, pending plus complete file remain for local-only recovery.
        self.finish(row)


def crawl(config: Config):
    stats = dict(boards=0, pages=0, archived=0, rejected=0, skipped=0, failed=0)
    started = time.monotonic()
    with crawl_lock(config):
        progress = Progress(config)
        progress.update(phase='initializing', stats=stats)
        LOG.info('crawl_started min_images=%d interval=%.2f timeout=%.2f retries=%d',
                 config.min_images, config.interval, config.timeout, config.retries)
        try:
            store = Store(config)
        except BaseException:
            progress.update(status='failed', phase='initializing_failed')
            raise
        try:
            LOG.info('recovery_started')
            progress.update(phase='recovery')
            store.recover()
            client = Client(config)
            client.progress = progress
            client.request_kind = 'home'
            progress.update(phase='home')
            LOG.info('home_started')
            root, base = client.page(config.url)
            style = forum_style(root, base, config.style)
            queue = deque(config.fids or board_ids(root, base))
            if not queue:
                raise ArchiveError('No boards discovered; check login, template, or configure forum.fids')
            LOG.info('boards_discovered count=%d style=%s', len(queue), style)
            seen_boards, seen_threads = set(), set()
            while queue:
                fid = queue.popleft()
                if fid in seen_boards or fid in config.exclude_fids:
                    continue
                seen_boards.add(fid)
                stats['boards'] += 1
                page, maximum = 1, 1
                sticky_proof = None
                reported_stickies = set()
                try:
                    while page <= maximum:
                        if page > config.max_pages:
                            raise ArchiveError('Board page limit reached; this scan is incomplete')
                        listing = board_url(base, style, fid, page)
                        client.request_kind = 'listing'
                        progress.update(phase='listing', fid=fid, tid=None, stats=stats)
                        LOG.info('board_page_started fid=%s page=%d known_last_page=%d', fid, page, maximum)
                        document, actual = client.page(listing, base)
                        stats['pages'] += 1
                        board_summaries = {n.attrs['summary'] for n in document.walk()
                                           if n.attrs.get('id') == 'threadlisttableid' and
                                           re.fullmatch(r'forum_\d+', n.attrs.get('summary', ''))}
                        if board_summaries and 'forum_'+fid not in board_summaries:
                            raise ArchiveError('Server returned a different board; refusing an incomplete/misdirected scan')
                        if not config.fids:
                            queue.extend(x for x in board_ids(document, actual) if x not in seen_boards)
                        maximum = max(maximum, last_page(document, fid, actual, page))
                        confirmed = digest_filter_active(document, actual, fid)
                        explicit = set(digest_threads(document, actual))
                        verified = set()
                        if confirmed:
                            uncertain = sticky_ids(document) - explicit
                            verified.update(tid for tid in uncertain if store.terminal(tid))
                            uncertain -= verified
                            if uncertain and sticky_proof is None:
                                client.request_kind = 'sticky_verification'
                                progress.update(phase='sticky_verification')
                                ordinary, ordinary_url = client.page(board_url(base, style, fid, 1, digest=False), actual)
                                sticky_proof = set(digest_threads(ordinary, ordinary_url))
                            verified.update(uncertain & (sticky_proof or set()))
                            unresolved = uncertain - verified
                            newly_unresolved = unresolved - reported_stickies
                            if newly_unresolved:
                                stats['failed'] += len(newly_unresolved)
                                LOG.error('unverified_digest_stickies fid=%s count=%d; not archived', fid, len(newly_unresolved))
                                reported_stickies.update(newly_unresolved)
                        tids = list(digest_threads(document, actual, confirmed, verified))
                        LOG.info('board_page_parsed fid=%s page=%d candidates=%d last_page=%d',
                                 fid, page, len(tids), maximum)
                        if not confirmed and not explicit:
                            raise ArchiveError('Server did not confirm digest filtering and no digest markers were found')
                        if not tids and not any(n.attrs.get('id') == 'threadlisttableid' or
                                                n.tag == 'table' and 'threadlist' in n.classes() or
                                                re.match(r'(?:normal|stick)thread_', n.attrs.get('id',''))
                                                for n in document.walk()):
                            raise ArchiveError('Cannot verify digest listing; login or unsupported template')
                        for tid in tids:
                            if tid in seen_threads:
                                continue
                            seen_threads.add(tid)
                            known = store.known(tid)
                            if known:
                                stats['skipped'] += 1
                                LOG.debug('history_skip tid=%s state=archived', tid)
                                continue
                            if store.rejection(tid):
                                stats['skipped'] += 1
                                LOG.debug('history_skip tid=%s state=rejected', tid)
                                continue
                            try:
                                client.request_kind = 'post'
                                progress.update(phase='post', tid=tid, image_attempts=0,
                                                image_available=0, image_failed=0, download_total=0,
                                                download_completed=0, download_failed=0, stats=stats)
                                LOG.info('post_started fid=%s tid=%s', fid, tid)
                                try:
                                    post, post_url = client.page(thread_url(base, style, tid), actual)
                                except FetchError as error:
                                    if config.min_images and error.status in {401, 403, 404, 410}:
                                        raise InsufficientImages(0, config.min_images, f'post_http_{error.status}') from None
                                    raise
                                title, data = extract_post(post, client, post_url)
                                progress.update(phase='saving')
                                store.save(tid, title, data)
                                stats['archived'] += 1
                                LOG.info('archived tid=%s bytes=%d', tid, len(data))
                            except InsufficientImages as error:
                                if store.reject(tid, error):
                                    stats['rejected'] += 1
                                LOG.info('permanently_rejected tid=%s available=%d required=%d reason=%s',
                                         tid, error.available, error.required, error.reason)
                            except ArchiveError as error:
                                stats['failed'] += 1
                                LOG.error('post_failed tid=%s reason=%s', tid, error)
                            progress.update(stats=stats)
                        page += 1
                except ArchiveError as error:
                    stats['failed'] += 1
                    LOG.error('board_failed fid=%s reason=%s', fid, error)
            stats['elapsed_ms'] = int((time.monotonic()-started)*1000)
            with store.db:
                store.db.execute("INSERT OR REPLACE INTO meta VALUES('last_run',?)", (json.dumps(stats),))
        finally:
            failed = sys.exc_info()[0] is not None
            progress.update(status='failed' if failed else 'finished',
                            phase='failed' if failed else 'finished', stats=stats)
            store.close()
    LOG.info('crawl_finished %s', json.dumps(stats, sort_keys=True))
    return stats


@contextmanager
def readonly_db(config: Config):
    if not config.db_path.is_file():
        raise ArchiveError('History database is missing; run the crawler first')
    db = sqlite3.connect(config.db_path.as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        yield db
    finally:
        db.close()


def rejected_count(db):
    # Readers can inspect v1 data without writing a migration or starting a crawl.
    if db.execute('PRAGMA user_version').fetchone()[0] == 1:
        return 0
    return db.execute('SELECT count(*) FROM rejected').fetchone()[0]


def check_local(config: Config):
    failures, missing, count = [], [], 0
    with readonly_db(config) as db:
        identity = db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
        marker = config.output_dir / '.discuz-archive.json'
        if not marker.is_file() or not identity or json.loads(marker.read_text()).get('identity') != identity[0]:
            failures.append('identity_mismatch')
        for row in db.execute('SELECT * FROM archives'):
            count += 1
            path = config.output_dir / row['filename']
            if Path(row['filename']).name != row['filename'] or path.is_symlink():
                failures.append('unsafe:' + row['tid'])
                continue
            if not path.exists():
                missing.append(row['tid'])
                continue
            if not path.is_file():
                failures.append('unsafe:' + row['tid'])
                continue
            try:
                with path.open('rb') as file:
                    digest = hashlib.file_digest(file, 'sha256').hexdigest()
                    actual_size = os.fstat(file.fileno()).st_size
            except FileNotFoundError:
                missing.append(row['tid'])
                continue
            if digest != row['sha256'] or actual_size != row['size']:
                failures.append('checksum_mismatch:' + row['tid'])
        pending = db.execute('SELECT count(*) FROM pending').fetchone()[0]
        rejected = rejected_count(db)
    print(json.dumps(dict(archives=count, rejected=rejected, pending=pending,
                         missing_files=missing, errors=failures), ensure_ascii=False))
    return not failures and not pending


ICON = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="12" fill="#39634d"/><path d="M18 16h28v34H18z" fill="#fff"/><path d="M24 25h16M24 32h16M24 39h10" stroke="#39634d" stroke-width="3"/></svg>'
SW = b'''const NAME='discuz-shell-v1';self.addEventListener('install',e=>e.waitUntil(caches.open(NAME).then(c=>c.addAll(['/offline','/icon.svg']))));self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k.startsWith('discuz-shell-')&&k!==NAME).map(k=>caches.delete(k))))));self.addEventListener('fetch',e=>{if(e.request.mode==='navigate'&&new URL(e.request.url).origin===self.location.origin)e.respondWith(fetch(e.request).catch(()=>caches.match('/offline')))});'''


def make_web_server(config: Config):
    token = ''
    if config.web_token_file:
        token = config.web_token_file.read_text('utf-8').strip()
        if len(token) < 24:
            raise ArchiveError('Web token must contain at least 24 characters')
    if config.web_host not in {'127.0.0.1', 'localhost', '::1'} and not token:
        raise ArchiveError('Remote Web binding requires web.token_file; use a local HTTPS reverse proxy')
    cache_lock = threading.Lock()
    cached_state = [0.0, {}]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):
            LOG.debug('web status=%s', str(args[1]) if len(args) > 1 else 'request')

        def send_data(self, data, mime='text/html; charset=utf-8', status=200, extra=None):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', CSP + "; script-src 'self'; worker-src 'self'; manifest-src 'self'; connect-src 'self'; img-src-elem 'self' data:")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(data)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if token:
                expected = 'Basic ' + base64.b64encode(('archive:'+token).encode()).decode()
                if not hmac.compare_digest(self.headers.get('Authorization', ''), expected):
                    self.send_data(b'Authentication required', 'text/plain', 401,
                                   {'WWW-Authenticate': 'Basic realm="Archive", charset="UTF-8"'})
                    return
            try:
                self.route()
            except (ArchiveError, sqlite3.Error, OSError, ValueError) as error:
                LOG.warning('web_read_failed type=%s', type(error).__name__)
                self.send_data('本地存档暂不可用，请检查历史及备份。'.encode(), 'text/plain; charset=utf-8', 503)

        def route(self):
            parts = urlsplit(self.path)
            path = parts.path
            if path == '/icon.svg':
                return self.send_data(ICON, 'image/svg+xml')
            if path == '/manifest.webmanifest':
                return self.send_data(json.dumps({'name':'帖子存档','short_name':'存档','lang':'zh-CN',
                    'start_url':'/','display':'standalone','background_color':'#f7f8f5','theme_color':'#39634d',
                    'icons':[{'src':'/icon.svg','sizes':'any','type':'image/svg+xml','purpose':'any'}]}, ensure_ascii=False).encode(), 'application/manifest+json')
            if path == '/sw.js':
                return self.send_data(SW, 'text/javascript; charset=utf-8', extra={'Service-Worker-Allowed':'/'})
            if path == '/app.js':
                return self.send_data(b"if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js').catch(()=>{});", 'text/javascript')
            if path == '/offline':
                return self.send_data(shell('离线阅读', '<h1>当前处于离线状态</h1><p>请打开已下载的 HTML 存档继续阅读。</p>'))
            if path == '/api/debug/state':
                with cache_lock:
                    if time.monotonic()-cached_state[0] > 5:
                        with readonly_db(config) as db:
                            latest = db.execute("SELECT value FROM meta WHERE key='last_run'").fetchone()
                            cached_state[:] = [time.monotonic(), dict(version=VERSION,
                                archives=db.execute('SELECT count(*) FROM archives').fetchone()[0],
                                rejected=rejected_count(db),
                                pending=db.execute('SELECT count(*) FROM pending').fetchone()[0],
                                last_run=json.loads(latest[0]) if latest else None)]
                    result = json.dumps(dict(cached_state[1], current_run=read_progress(config))).encode()
                return self.send_data(result, 'application/json')
            if path == '/':
                params = parse_qs(parts.query)
                value = params.get('page', ['1'])[0]
                if not value.isdecimal() or not 1 <= int(value) <= 1000000:
                    return self.send_data(b'Invalid page', 'text/plain', 400)
                page = int(value)
                try:
                    with readonly_db(config) as db:
                        count = db.execute('SELECT count(*) FROM archives').fetchone()[0]
                        rows = db.execute('SELECT id,title,archived_at FROM archives ORDER BY archived_at DESC,id DESC LIMIT 50 OFFSET ?', ((page-1)*50,)).fetchall()
                except ArchiveError:
                    if (config.db_path.exists() or (config.output_dir / '.discuz-archive.json').exists()
                            or any(config.output_dir.glob('*.html'))):
                        raise
                    count, rows = 0, []
                content = '<p class="label">离线阅读</p><h1>帖子存档</h1><p class="muted">共 ' + str(count) + ' 篇</p><ul class="posts">'
                content += ''.join('<li><a href="/post/' + str(r['id']) + '"><span>' + html.escape(r['title']) +
                                   '</span><time>' + html.escape(r['archived_at'][:10]) + '</time></a></li>' for r in rows)
                content += '</ul>' + ('<p class="muted">还没有存档。</p>' if not count else '') + '<nav>'
                if page > 1:
                    content += f'<a href="/?page={page-1}">上一页</a>'
                if page*50 < count:
                    content += f'<a href="/?page={page+1}">下一页</a>'
                content += '</nav><footer><details><summary>关于</summary><p>本地首次存档，只读浏览。内容版权归原作者所有。作者：***。</p></details></footer>'
                return self.send_data(shell('帖子存档', content, web=True))
            match = re.fullmatch(r'/post/(\d+)', path)
            if match:
                with readonly_db(config) as db:
                    row = db.execute('SELECT * FROM archives WHERE id=?', (int(match[1]),)).fetchone()
                if row:
                    filename = row['filename']
                    file = config.output_dir / filename
                    if Path(filename).name != filename or file.is_symlink():
                        raise ArchiveError('Unsafe local archive path')
                    if not file.is_file():
                        return self.send_data('此存档已移出本地目录。'.encode(), 'text/plain; charset=utf-8', 404)
                    disposition = 'attachment' if parse_qs(parts.query).get('download') == ['1'] else 'inline'
                    # Stream instead of materializing potentially large embedded GIF archives.
                    try:
                        src = file.open('rb')
                    except FileNotFoundError:
                        return self.send_data('此存档已移出本地目录。'.encode(), 'text/plain; charset=utf-8', 404)
                    with src:
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html; charset=utf-8')
                        self.send_header('Content-Length', str(os.fstat(src.fileno()).st_size))
                        self.send_header('Content-Disposition', disposition + "; filename*=UTF-8''" + quote(filename))
                        self.send_header('Content-Security-Policy', CSP + '; sandbox')
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('Referrer-Policy', 'no-referrer')
                        self.send_header('X-Content-Type-Options', 'nosniff')
                        self.send_header('X-Frame-Options', 'DENY')
                        self.end_headers()
                        if self.command != 'HEAD':
                            shutil.copyfileobj(src, self.wfile, length=128*1024)
                    return
            self.send_data(b'Not found', 'text/plain', 404)

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True
        if ':' in config.web_host:
            address_family = socket.AF_INET6

    return Server((config.web_host, config.web_port), Handler)


def serve(config: Config):
    server = make_web_server(config)
    LOG.info('reader_started port=%d', server.server_port)
    print(f'Reader: http://{config.web_host}:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('config.toml'))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--serve', action='store_true', help='Serve the local read-only archive list')
    mode.add_argument('--check', action='store_true', help='Verify local files and history without network requests')
    mode.add_argument('--install', action='store_true', help='Install daily systemd timer and optional reader')
    mode.add_argument('--uninstall', action='store_true', help='Remove owned systemd units; preserve all archives/history')
    parser.add_argument('--dry-run', action='store_true', help='Print planned systemd changes without applying them')
    parser.add_argument('--version', action='version', version=VERSION)
    args = parser.parse_args(argv)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLog())
    LOG.handlers[:] = [handler]
    LOG.setLevel(logging.INFO)
    try:
        if args.dry_run and not (args.install or args.uninstall):
            parser.error('--dry-run requires --install or --uninstall')
        if args.uninstall:
            # Removal deliberately works even when the original config is missing or broken.
            install_service(None, args.config, uninstall=True, dry_run=args.dry_run)
            return 0
        config = Config.from_file(args.config)
        LOG.setLevel(config.log_level)
        if args.install:
            install_service(config, args.config, dry_run=args.dry_run)
        elif args.serve:
            serve(config)
        elif args.check:
            return 0 if check_local(config) else 1
        else:
            return 1 if crawl(config)['failed'] else 0
        return 0
    except (ArchiveError, RuntimeError, OSError, ValueError, sqlite3.Error, tomllib.TOMLDecodeError) as error:
        LOG.error('operation_failed reason=%s', str(error))
        return 1
    except KeyboardInterrupt:
        LOG.warning('interrupted; committed archives remain immutable')
        return 130


# systemd integration is embedded below; no external runtime modules are needed.

# Standard-library-only systemd integration, intended to be embedded in discuz_archive.py.
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tempfile

UNIT_MARKER = '# Managed by discuz-archive'
UNIT_DIRECTORY = Path('/etc/systemd/system')
UNIT_NAMES = ('discuz-archive.service', 'discuz-archive.timer', 'discuz-archive-web.service')


def unit_quote(value: str) -> str:
    """Quote one systemd word; ExecStart also disables environment expansion."""
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('systemd values must not contain control characters')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def build_units(config_path: Path, script_path: Path, python_path: str,
                run_user: str, calendar: str, web_enabled: bool) -> dict[str, str]:
    """Generate unit files without making any system changes."""
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*[$]?', run_user) or run_user == 'root':
        raise ValueError('service user must be an ordinary, non-root system username')
    if not calendar.strip() or any(ord(c) < 32 or ord(c) == 127 for c in calendar):
        raise ValueError('schedule must be one nonempty systemd calendar expression')
    if '%' in calendar or '\\' in calendar:
        raise ValueError('schedule may not contain unit substitutions or continuations')
    config_path = config_path.resolve()
    script_path = script_path.resolve()
    if not Path(python_path).is_absolute():
        raise ValueError('service Python interpreter must be an absolute path')
    command = ':' + ' '.join(unit_quote(str(x)) for x in
                             (python_path, script_path, '--config', config_path))
    common = (f'User={run_user}\n'
              f'WorkingDirectory={unit_quote(str(config_path.parent))}\n'
              'UMask=0077\n'
              'NoNewPrivileges=true\n'
              'PrivateTmp=true\n'
              'ProtectSystem=full\n'
              'ProtectKernelTunables=true\n'
              'ProtectKernelModules=true\n'
              'ProtectControlGroups=true\n'
              'RestrictSUIDSGID=true\n'
              'Environment=PYTHONDONTWRITEBYTECODE=1\n'
              'StandardOutput=journal\n'
              'StandardError=journal\n'
              'TimeoutStopSec=45s\n')
    files = {
        'discuz-archive.service': (
            f'{UNIT_MARKER}\n[Unit]\nDescription=Discuz first-copy archive crawler\n'
            'Wants=network-online.target\nAfter=network-online.target\n\n'
            '[Service]\nType=oneshot\n' + common + f'ExecStart={command}\n'
            'TimeoutStartSec=infinity\n'),
        'discuz-archive.timer': (
            f'{UNIT_MARKER}\n[Unit]\nDescription=Daily Discuz archive crawl\n\n'
            f'[Timer]\nOnCalendar={calendar}\nPersistent=true\n'
            'Unit=discuz-archive.service\n\n[Install]\nWantedBy=timers.target\n'),
    }
    if web_enabled:
        files['discuz-archive-web.service'] = (
            f'{UNIT_MARKER}\n[Unit]\nDescription=Discuz local archive reader\n'
            'After=network.target\n\n[Service]\nType=exec\n' + common +
            f'ExecStart={command} "--serve"\nRestart=on-failure\nRestartSec=5s\n\n'
            '[Install]\nWantedBy=multi-user.target\n')
    return files


def _managed_units(directory: Path = UNIT_DIRECTORY) -> dict[str, Path]:
    """Refuse collisions before changing even one of the installed units."""
    found = {}
    for name in UNIT_NAMES:
        path = directory / name
        if path.is_symlink():
            raise RuntimeError(f'Refusing to change a symlinked unit: {name}')
        if path.exists():
            if not path.is_file() or path.read_text(encoding='utf-8').splitlines()[:1] != [UNIT_MARKER]:
                raise RuntimeError(f'Refusing to change a unit not owned by this tool: {name}')
            found[name] = path
    return found


def _systemctl(*args: str, required: bool = True) -> subprocess.CompletedProcess:
    executable = shutil.which('systemctl')
    if not executable:
        raise RuntimeError('systemctl is required for installation or uninstallation')
    result = subprocess.run([executable, *args], capture_output=True, text=True, timeout=90)
    if required and result.returncode:
        # Avoid forwarding arbitrary daemon output that could include configured URLs.
        raise RuntimeError(f'systemctl {args[0]} failed (exit {result.returncode}); inspect the system journal')
    return result


def _service_account() -> str:
    candidate = os.environ.get('SUDO_USER', '')
    if not candidate or candidate == 'root':
        candidate = 'discuz-archive'
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*[$]?', candidate):
        raise RuntimeError('Invalid SUDO_USER; use an ordinary non-root service account')
    return candidate


def service_access_code() -> str:
    """Read-only probe run as the actual service account; argv keeps n,d,paths."""
    return r'''
import json
import os
from pathlib import Path
import sys

n, d = int(sys.argv[1]), int(sys.argv[2])
paths = sys.argv[3:]
readable, directories, files = paths[:n], paths[n:n+d], paths[n+d:]
failures = []
seen = set()

def issue(operation, path, target):
    record = (operation, str(path), str(target))
    if record not in seen:
        seen.add(record)
        if len(failures) < 64:
            failures.append(dict(operation=operation, path=str(path), target=str(target)))

def parents_clear(path):
    for parent in reversed(path.parents):
        if not os.path.isdir(parent):
            issue('missing', parent, path)
            return False
        if not os.access(parent, os.X_OK):
            issue('traverse', parent, path)
            return False
    return True

def check(raw, operation, mode, directory=False):
    path = Path(os.path.abspath(raw))
    if not parents_clear(path):
        return
    if not os.path.exists(path):
        issue('missing', path, path)
        return
    if directory and not os.path.isdir(path):
        issue('directory', path, path)
        return
    if not directory and not os.path.isfile(path):
        issue('file', path, path)
        return
    if not os.access(path, mode):
        issue(operation, path, path)

for path in readable:
    check(path, 'read', os.R_OK)
for path in directories:
    check(path, 'traverse', os.X_OK, directory=True)
    check(path, 'write', os.W_OK, directory=True)
for path in files:
    check(path, 'write', os.W_OK)
if len(readable) >= 3:
    check(readable[2], 'execute', os.X_OK)
if readable:
    check(str(Path(os.path.abspath(readable[0])).parent), 'traverse', os.X_OK, directory=True)
print(json.dumps(dict(uid=os.geteuid(), failures=failures, total_failures=len(seen)), ensure_ascii=True))
sys.exit(3 if seen else 0)
'''


def format_service_access_error(user: str, stdout, python_path: str) -> str:
    """Format only trusted schema fields; never echo launcher output or secrets."""
    def safe(value):
        return ''.join(character if ord(character) >= 32 and ord(character) != 127 else '?'
                       for character in str(value))[:4096]

    try:
        report = json.loads(stdout)
        failures = report.get('failures') if isinstance(report, dict) else None
    except (TypeError, ValueError, UnicodeError):
        failures = None
    prefix = f"Service account '{safe(user)}' failed access checks."
    if not isinstance(failures, list) or not failures:
        return (prefix + f" Python interpreter '{safe(python_path)}' could not start as this account; "
                'check its execute permission, traversable parent directories and runtime dependencies. '
                'Use namei -l on the interpreter and installation paths; do not run the service as root.')
    labels = {'read': 'cannot read file', 'execute': 'cannot execute interpreter',
              'traverse': 'cannot traverse directory', 'write': 'cannot write',
              'missing': 'path missing or inaccessible', 'file': 'expected a regular file',
              'directory': 'expected a directory'}
    lines = []
    for entry in failures:
        if not isinstance(entry, dict) or entry.get('operation') not in labels:
            continue
        path, target = entry.get('path'), entry.get('target')
        if not isinstance(path, str) or not isinstance(target, str):
            continue
        line = f"{labels[entry['operation']]}: {safe(path)}"
        if path != target:
            line += f" (required for {safe(target)})"
        if line not in lines:
            lines.append(line)
        if len(lines) == 8:
            break
    if not lines:
        return prefix + ' Permission probe returned no usable diagnostics; inspect interpreter execution and parent-directory traversal.'
    return (prefix + '\n- ' + '\n- '.join(lines) +
            '\nGrant only the required access to this account or move the installation to an accessible persistent directory. '
            'Existing data owned by another ordinary user must be explicitly migrated by the administrator.')


def _ensure_service_access(config, config_path: Path, script_path: Path,
                           python_path: str, user: str) -> None:
    try:
        account = pwd.getpwnam(user)
    except KeyError:
        if user != 'discuz-archive':
            raise RuntimeError('The requested service user does not exist') from None
        useradd = shutil.which('useradd')
        if not useradd:
            raise RuntimeError('useradd is required to create the service account')
        subprocess.run([useradd, '--system', '--user-group', '--no-create-home',
                        '--home-dir', '/nonexistent', '--shell', '/usr/sbin/nologin', user],
                       check=True, capture_output=True, timeout=30)
        account = pwd.getpwnam(user)
    if account.pw_uid == 0:
        raise RuntimeError('Archive services must not run as root')
    writable = [Path(config.output_dir).resolve(), Path(config.db_path).resolve().parent]
    for path in writable:
        protected = (Path('/usr'), Path('/etc'), Path('/boot'), Path('/efi'))
        if any(path == base or base in path.parents for base in protected):
            raise RuntimeError('Data directories must be outside systemd read-only system paths')
        if path in (Path('/'), Path('/var'), config_path.parent):
            raise RuntimeError('Use dedicated output and state subdirectories before installing')
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.stat().st_uid == 0:
            # Change only the two dedicated data directories, never the checkout tree.
            os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)
    database = Path(config.db_path).resolve()
    state_files = [Path(str(database) + suffix) for suffix in ('', '-wal', '-shm', '.lock')]
    state_files.append(database.parent / 'progress.json')
    output_dir = Path(config.output_dir).resolve()
    output_files = [output_dir / '.discuz-archive.json',
                    *output_dir.glob('*.html'), *output_dir.glob('.pending-*.tmp')]
    for file in [*state_files, *output_files]:
        if file.is_symlink():
            raise RuntimeError('Service data must not contain symlinked archive or state files')
        if file.exists() and file.is_file() and file.stat().st_uid == 0:
            # Only known direct files are migrated; never traverse or take over other users' files.
            os.chown(file, account.pw_uid, account.pw_gid, follow_symlinks=False)
    readable = [config_path, script_path, Path(python_path)]
    for field in ('cookie_file', 'web_token_file'):
        secret = getattr(config, field, None)
        if secret:
            readable.append(Path(secret).resolve())
    readable += [p for p in output_files if p.exists()]
    writable_files = [p for p in state_files if p.exists()]
    writable_files += [p for p in output_files if p.name.startswith('.pending-') and p.exists()]
    paths = [*readable, *writable, *writable_files]
    for path in paths:
        if any(path == prefix or prefix in path.parents for prefix in (Path('/tmp'), Path('/var/tmp'))):
            raise RuntimeError('Install from persistent storage such as /opt; PrivateTmp hides /tmp')
    runuser = shutil.which('runuser')
    if not runuser:
        raise RuntimeError('runuser is required to verify service account permissions')
    check_code = service_access_code()
    LOG.info('service_access_check user=%s', user)
    result = subprocess.run([runuser, '-u', user, '--', python_path, '-c', check_code,
                             str(len(readable)), str(len(writable)), *(str(p) for p in paths)],
                            capture_output=True, timeout=30, cwd='/')
    if result.returncode:
        try:
            report = json.loads(result.stdout)
        except (ValueError, TypeError, UnicodeError):
            report = None
        output = result.stdout
        if not isinstance(report, dict) and python_path != '/usr/bin/python3' and Path('/usr/bin/python3').is_file():
            fallback = subprocess.run([runuser, '-u', user, '--', '/usr/bin/python3', '-c', check_code,
                                       str(len(readable)), str(len(writable)), *(str(p) for p in paths)],
                                      capture_output=True, timeout=30, cwd='/')
            # A successful fallback checks metadata; it cannot prove the configured
            # interpreter can start, so preserve the original installation failure.
            output = fallback.stdout
        raise RuntimeError(format_service_access_error(user, output, python_path))


def _write_unit(path: Path, content: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix='.discuz-unit-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install_service(config, config_path: Path, *, uninstall: bool = False,
                    dry_run: bool = False) -> None:
    """Install/remove only owned units; all archive/config/database files survive."""
    config_path = config_path.resolve()
    if uninstall and dry_run:
        print('Would stop and remove owned units: ' + ', '.join(UNIT_NAMES))
        return
    user = _service_account()
    script_path = Path(__file__).resolve()
    python_path = str(Path(sys.executable).resolve())
    units = None if uninstall else build_units(config_path, script_path, python_path, user,
                                               config.schedule, config.web_enabled)
    if dry_run:
        for name, content in units.items():
            print(f'### {name}\n{content}')
        return
    if sys.platform != 'linux' or not Path('/run/systemd/system').is_dir():
        raise RuntimeError('Installation requires Linux booted with systemd (Debian/Ubuntu)')
    if os.geteuid() != 0:
        raise RuntimeError('Installing/removing system units requires sudo or root; ordinary crawling does not')
    existing = _managed_units(UNIT_DIRECTORY)
    if uninstall:
        if not existing:
            print('No managed service units are installed; archive data was preserved.')
            return
        for name in ('discuz-archive.timer', 'discuz-archive-web.service'):
            if name in existing:
                _systemctl('disable', '--now', name)
        if 'discuz-archive.service' in existing:
            _systemctl('stop', 'discuz-archive.service')
        if 'discuz-archive.timer' in existing:
            _systemctl('clean', '--what=state', 'discuz-archive.timer', required=False)
        for path in existing.values():
            path.unlink()
        _systemctl('daemon-reload')
        _systemctl('reset-failed', *(name for name in existing), required=False)
        print('Managed service units removed; config, archive files and history were preserved.')
        return
    _ensure_service_access(config, config_path, script_path, python_path, user)
    analyze = shutil.which('systemd-analyze')
    if analyze:
        result = subprocess.run([analyze, 'calendar', config.schedule], capture_output=True, timeout=15)
        if result.returncode:
            raise RuntimeError('Invalid systemd schedule; check schedule with systemd-analyze calendar')
        with tempfile.TemporaryDirectory(prefix='discuz-unit-check-') as temporary:
            check_paths = []
            for name, content in units.items():
                path = Path(temporary) / name
                path.write_text(content, encoding='utf-8')
                check_paths.append(str(path))
            result = subprocess.run([analyze, 'verify', *check_paths],
                                    capture_output=True, timeout=30)
            if result.returncode:
                raise RuntimeError('Generated systemd unit validation failed; no unit files were changed')
    # Stop old work before replacing units; reinstalling never races a running crawl.
    for name in ('discuz-archive.timer', 'discuz-archive-web.service'):
        if name in existing:
            _systemctl('stop', name)
    if 'discuz-archive.service' in existing:
        _systemctl('stop', 'discuz-archive.service')
    if 'discuz-archive-web.service' in existing and not config.web_enabled:
        _systemctl('disable', 'discuz-archive-web.service')
        existing['discuz-archive-web.service'].unlink()
    for name, content in units.items():
        _write_unit(UNIT_DIRECTORY / name, content)
    _systemctl('daemon-reload')
    _systemctl('enable', '--now', 'discuz-archive.timer')
    # restart applies a changed calendar to an already enabled timer as well.
    _systemctl('restart', 'discuz-archive.timer')
    if config.web_enabled:
        _systemctl('enable', '--now', 'discuz-archive-web.service')
    print(f'Installed daily archive timer and optional reader as {user}.')


if __name__ == "__main__":
    raise SystemExit(main())
