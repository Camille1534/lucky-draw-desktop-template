"""HTTP integration checks using temporary data, never the operator's database."""
import importlib.util
import gc
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
urlopen = build_opener(ProxyHandler({})).open
sys.path.insert(0, str(ROOT))


class EventIntegration(unittest.TestCase):
    def test_isolation_restart_assets_and_install_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            os.environ['LUCKY_DRAW_DATA_DIR'] = folder
            os.environ['LUCKY_DRAW_UPDATE_TOKEN'] = 'test-update-secret'
            spec = importlib.util.spec_from_file_location('draw_test_server', ROOT / 'server.py')
            server = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(server)
            server.init_db()
            httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{httpd.server_port}'
            token = ''
            active = 'default'

            def call(path, body=None, status=200, event=None, update=None):
                headers = {'Content-Type': 'application/json', 'X-Admin-Token': token,
                           'X-Event-Id': active if event is None else event}
                if update:
                    headers['X-Update-Token'] = update
                request = Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
                try:
                    response = urlopen(request)
                except HTTPError as error:
                    response = error
                with response:
                    self.assertEqual(response.status, status, path)
                    return json.loads(response.read())

            try:
                token = call('/api/login', {'password': 'admin123'})['token']
                call('/api/guest/add', {'guest_id':'same', 'name':'Original', 'session_id':1})
                config = {'name':'Second Event', 'accent':'#112233', 'date':'2026', 'tagline':'Hello'}
                second = call('/api/events/create', config)['id']
                self.assertEqual(call('/api/state')['event']['id'], 'default')
                call('/api/events/switch', {'id':second})
                active = second
                self.assertEqual(call('/api/state')['guests'], [])
                call('/api/guest/add', {'guest_id':'same','name':'Second','session_id':1})
                call('/api/guest/check', {'guest_id':'same','checked':True}, event='default', status=409)
                self.assertFalse(call('/api/state')['guests'][0]['checked'])
                call('/api/guest/check', {'guest_id':'same','checked':True})
                call('/api/draw', {'session_id':1})
                call('/api/events/switch', {'id':'default'}, status=400)
                call('/api/updates/prepare', {}, status=403, update='wrong')
                call('/api/updates/prepare', {}, status=409, update='test-update-secret')
                call('/api/draw/pause', {'session_id':1})
                call('/api/confirm', {'session_id':1})
                self.assertEqual(sum(p['used_count'] for p in call('/api/state')['prizes']), 1)
                self.assertEqual(len(call('/api/state')['records']), 1)
                call('/api/events/update', {**config, 'id':second, 'logo':'data:image/png;base64,bad'}, status=400)
                png = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jC1sAAAAASUVORK5CYII='
                call('/api/events/update', {**config, 'id':second, 'logo':png})
                asset = call('/api/state')['event']['logo']
                with urlopen(base+asset) as response:
                    self.assertEqual(response.headers['Content-Type'], 'image/png')
                    self.assertTrue(response.read().startswith(b'\x89PNG'))
                call('/api/events/switch', {'id':'default'})
                active = 'default'
                original = call('/api/state')
                self.assertEqual(original['guests'][0]['name'], 'Original')
                self.assertEqual(original['records'], [])
                self.assertEqual(sum(p['used_count'] for p in original['prizes']), 0)
                call('/api/events/switch', {'id':second})
                active = second
                server.EVENTS = server.EventStore(folder)
                server.DB_PATH = server.EVENTS.database(server.EVENTS.active())
                server.init_db()
                self.assertEqual(call('/api/state')['event']['id'], second)
                self.assertEqual(len(call('/api/state')['records']), 1)
                self.assertTrue((Path(folder)/'lucky-draw.sqlite').exists())
                call('/api/updates/prepare', {}, update='test-update-secret')
                call('/api/guest/add', {'guest_id':'late','name':'Late'}, status=409)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join()
                gc.collect()


if __name__ == '__main__':
    unittest.main()
