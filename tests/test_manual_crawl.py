"""Manual crawl security and real neutral localhost integration."""
import base64
from dataclasses import replace
from http.client import HTTPConnection
import json
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import discuz_archive as a
from tests.test_discuz_archive import FixtureState, fixture_server
from tests.test_web_base_path import running_reader


class ManualTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        token = root/'token'
        token.write_text('a'*32)
        self.config = a.Config('manual-neutral', 'https://forum.example.org/index.php',
                               root/'output', root/'data/history.sqlite3',
                               web_port=0, web_base_path='/archive', web_token_file=token)
        self.auth = 'Basic '+base64.b64encode(('archive:'+'a'*32).encode()).decode()

    def request(self, port, path='/archive/', method='GET', body=None, headers=None, auth=True):
        c = HTTPConnection('127.0.0.1', port, timeout=5)
        h = {'Authorization': self.auth} if auth else {}
        h.update(headers or {})
        try:
            c.request(method,path,body=body,headers=h)
            r=c.getresponse()
            return r.status,dict(r.getheaders()),r.read()
        finally:
            c.close()

    def token(self, port):
        status,_,body=self.request(port)
        self.assertEqual(status,200)
        return re.search(rb'name="csrf" value="([^"]+)"',body)[1].decode()

    def post(self,port,token,**kw):
        headers={'Content-Type':'application/x-www-form-urlencoded'}
        headers.update(kw.pop('headers',{}))
        return self.request(port,'/archive/api/crawl','POST',urlencode({'csrf':token}),headers,**kw)

    def test_security_boundaries_and_get_never_triggers(self):
        with running_reader(self.config) as port, patch.object(a.ManualCrawl,'start') as start:
            token=self.token(port)
            self.assertEqual(self.post(port,token,auth=False)[0],401)
            self.assertEqual(self.post(port,'wrong')[0],403)
            self.assertEqual(self.post(port,'无效令牌')[0],403)
            self.assertEqual(self.post(port,token,headers={'Origin':'https://evil.example'})[0],403)
            self.assertEqual(self.post(port,token,headers={'Origin':'null'})[0],403)
            self.assertEqual(self.post(port,token,headers={'Sec-Fetch-Site':'cross-site'})[0],403)
            self.assertEqual(self.request(port,'/archive/api/crawl')[0],404)
            self.assertEqual(self.request(port,'/api/crawl','POST','')[0],404)
            self.assertEqual(self.post(port,token,headers={'Content-Type':'application/json'})[0],400)
            self.assertEqual(self.request(port,'/archive/api/crawl','POST','x'*1025,
                {'Content-Type':'application/x-www-form-urlencoded'})[0],400)
            start.assert_not_called()
            self.assertEqual(self.post(port,token)[0],303)
            start.assert_called_once()

    def test_lock_reservation_duplicate_and_exception_release(self):
        entered,finish=threading.Event(),threading.Event()
        def task(config,**kw):
            self.assertTrue(kw['_lock_held'])
            entered.set()
            finish.wait(5)
            raise a.ArchiveError('neutral failure')
        with running_reader(self.config) as port, patch.object(a,'crawl',side_effect=task):
            token=self.token(port)
            with a.crawl_lock(self.config):
                self.assertEqual(self.post(port,token)[0],409)
            self.assertEqual(self.post(port,token)[0],303)
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.post(port,token)[0],409)
            self.assertTrue(json.loads(self.request(port,'/archive/api/crawl/state')[2])['running'])
            finish.set()
            self.wait_finished()
            self.assertIn('失败',a.crawl_status(self.config)['message'])
            with a.crawl_lock(self.config):
                pass

    def wait_finished(self):
        deadline=time.monotonic()+6
        while a.crawl_is_running(self.config) and time.monotonic()<deadline:
            time.sleep(.02)
        self.assertFalse(a.crawl_is_running(self.config))

    def test_real_http_trigger_archive_and_first_snapshot(self):
        state=FixtureState(pages={'19:1':['101']})
        with fixture_server(state) as base:
            self.config=replace(self.config,url=base+'index.php',fids=['19'],interval=0,timeout=3,retries=0)
            with running_reader(self.config) as port:
                token=self.token(port)
                status,headers,_=self.post(port,token)
                self.assertEqual(status,303)
                self.assertEqual(headers['Location'],'/archive/')
                self.wait_finished()
                progress=a.read_progress(self.config)
                self.assertEqual(progress['status'],'finished')
                self.assertEqual(progress['stats']['archived'],1)
                files=list(self.config.output_dir.glob('*.html'))
                self.assertEqual(len(files),1)
                original=files[0].read_bytes()
                state.edited=True
                self.assertEqual(self.post(port,token)[0],303)
                self.wait_finished()
                self.assertEqual(a.read_progress(self.config)['stats']['skipped'],1)
                self.assertEqual(files[0].read_bytes(),original)

    def test_thread_start_failure_releases_lock_and_stale_progress_is_not_running(self):
        with patch.object(threading.Thread,'start',side_effect=RuntimeError('neutral')):
            with self.assertRaises(RuntimeError):
                a.ManualCrawl(self.config).start()
        self.assertFalse(a.crawl_status(self.config)['running'])
        self.assertIn('中断',a.crawl_status(self.config)['message'])
        with a.crawl_lock(self.config):
            pass


if __name__=='__main__':
    unittest.main()
