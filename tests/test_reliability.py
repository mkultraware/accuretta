import contextvars
import hashlib
import http.client
import json
from pathlib import Path
import re
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

import bridge
from _patching import bridge_patch
from tests._sandbox import sandbox_user_files
from task_state import UndoStore, file_revision
from network_policy import offline_socket_guard


class UndoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = UndoStore(self.root / 'undo')

    def edit(self, path, content):
        self.store.snapshot(str(path))
        path.write_bytes(content)
        self.store.checkpoint(str(path))

    def test_concurrent_chats_and_restart(self):
        a, b = self.root / 'a.txt', self.root / 'b.txt'
        a.write_bytes(b'old\r\n')
        ca, cb = contextvars.Context(), contextvars.Context()
        ca.run(self.store.begin, 'a')
        cb.run(self.store.begin, 'b')
        ca.run(self.edit, a, b'new\n')
        cb.run(self.edit, b, b'created')
        first, second = ca.run(self.store.commit), cb.run(self.store.commit)
        reopened = UndoStore(self.root / 'undo')
        self.assertTrue(reopened.restore(first['turn_id'])['ok'])
        self.assertEqual(a.read_bytes(), b'old\r\n')
        self.assertEqual(b.read_bytes(), b'created')
        self.assertTrue(reopened.restore(second['turn_id'])['ok'])
        self.assertFalse(b.exists())

    def test_partial_conflict_preserves_file_and_retry(self):
        a, b = self.root / 'a', self.root / 'b'
        a.write_bytes(b'original')
        self.store.begin('chat')
        self.edit(a, b'agent')
        self.edit(b, b'new')
        summary = self.store.commit()
        a.write_bytes(b'human')
        result = self.store.restore(summary['turn_id'])
        self.assertEqual(result['restored'], 1)
        self.assertEqual(result['remaining'], 1)
        self.assertEqual(a.read_bytes(), b'human')
        self.assertFalse(b.exists())
        a.write_bytes(b'agent')
        self.assertTrue(self.store.restore(summary['turn_id'])['ok'])
        self.assertEqual(a.read_bytes(), b'original')

    def test_interrupted_write_is_preserved(self):
        path = self.root / 'a'
        path.write_text('old')
        turn = self.store.begin('chat')
        self.store.snapshot(str(path))
        path.write_text('partial')
        result = UndoStore(self.root / 'undo').restore(turn)
        self.assertFalse(result['ok'])
        self.assertEqual(path.read_text(), 'partial')


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'file.py'
        self.path.write_text('value = 1\n')
        self.debt = [{'path': str(self.path), 'sha256': file_revision(self.path), 'target': 'host', 'status': 'unchecked'}]
        self.target = bridge_patch('_selected_execution_target', return_value='host')
        self.target.start()
        self.addCleanup(self.target.stop)

    def test_syntax_keeps_debt(self):
        debt, changed = bridge._update_verification_debt(self.debt, 'check_syntax', {'path': str(self.path)}, {'ok': True, 'verification': {'sha256': self.debt[0]['sha256']}})
        self.assertTrue(changed)
        self.assertEqual(len(debt), 1)
        self.assertEqual(debt[0]['status'], 'syntax_checked')

    def test_full_runner_recognition(self):
        self.assertTrue(bridge._full_test_run('python -m pytest -q', '12 passed in 1s'))
        self.assertTrue(bridge._full_test_run('python -m unittest discover -v', 'Ran 3 tests in 0.02s\n\nOK\n'))
        for command, output in [('echo "1 passed"', '1 passed'), ('python -m pytest tests/test_one.py', '1 passed'), ('python -m pytest -k quick', '1 passed'), ('python -m pytest', 'no tests ran'), ('python -c "print(123)"', '1 passed')]:
            self.assertFalse(bridge._full_test_run(command, output), command)

    def test_matching_run_only_clears_unchanged_revision(self):
        result = {'ok': True, 'verification': {'tests_ran': True, 'target': 'host', 'revisions': {bridge._verification_key(str(self.path)): self.debt[0]['sha256']}}}
        unrelated = {'path': str(self.path.parent / 'other.py'), 'sha256': 'abc', 'target': 'host'}
        debt, _ = bridge._update_verification_debt(self.debt + [unrelated], 'run_tests', {}, result)
        self.assertEqual(debt, [unrelated])
        self.path.write_text('value = 2')
        debt, changed = bridge._update_verification_debt(self.debt, 'run_tests', {}, result)
        self.assertFalse(changed)
        self.assertEqual(debt, self.debt)

    def test_large_ledger_is_not_truncated(self):
        debt = [{'path': f'file{i}.py'} for i in range(120)]
        result, _ = bridge._update_verification_debt(debt, 'read_file', {}, {'ok': True})
        self.assertEqual(len(result), 120)

    def test_ledger_survives_real_journal_recovery(self):
        # SANDBOXED: this test previously wrote a 'private-ledger-recovery'
        # chat + turn journal into the LIVE data dir — the next bridge boot
        # recovered it and a phantom "new session (interrupted)" row appeared
        # in the owner's rail.
        import tempfile
        from pathlib import Path as _Path
        sandbox = _Path(tempfile.mkdtemp())
        chats_file = sandbox / "chats.json"
        chats_file.write_text("{}", encoding="utf-8")
        journal_dir = sandbox / "turn_journal"
        journal_dir.mkdir()
        chat_id = 'private-ledger-recovery'
        debt = [{'path': str(self.path.parent / f'file{i}.py')} for i in range(120)]
        with bridge_patch("CHATS_FILE", chats_file), \
             bridge_patch("TURN_JOURNAL_DIR", journal_dir):
            snapshot = bridge.get_chats()
            snapshot['chats'][chat_id] = {'id': chat_id, 'messages': []}
            snapshot['order'].append(chat_id)
            bridge.save_json(bridge.CHATS_FILE, snapshot)
            turn = bridge._turn_journal_begin(chat_id)
            bridge._turn_journal_checkpoint(chat_id, turn, [{'role': 'tool', 'name': 'write_file', 'content': 'done'}], verification_debt=debt)
            self.assertEqual(len(bridge.load_json(bridge._turn_journal_path(chat_id), {})['verification_debt']), 120)
            self.assertEqual(bridge._recover_turn_journals()['recovered'], 1)
            restored = bridge.get_chats()['chats'][chat_id]
            self.assertEqual(len(restored['verification_debt']), 120)
            self.assertEqual(restored['continuity']['verification_required_count'], 120)
        live = bridge.get_chats()
        self.assertNotIn('private-ledger-recovery', live['chats'],
                         'test chats must never land in the live rail')


class HTTPTests(unittest.TestCase):

    def setUp(self):
        sandbox_user_files(self)

    @classmethod
    def setUpClass(cls):
        class QuietHandler(bridge.Handler):
            def log_message(self, *args): pass
        cls.server = bridge.AccurettaHTTPServer(('127.0.0.1', 0), QuietHandler)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            conn.request(method, path, body, headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally: conn.close()

    def test_bootstrap_and_strict_mutations(self):
        status, headers, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        token = re.search(rb'name="accuretta-request-token" content="([^"]+)"', body)[1].decode()
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertEqual(self.request('POST', '/api/settings', '{}')[0], 403)
        auth = {'X-Accuretta-Token': token, 'Content-Type': 'application/json'}
        self.assertEqual(self.request('POST', '/api/settings', '{}', auth)[0], 200)
        self.assertEqual(self.request('POST', '/api/settings', '{}', {**auth, 'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.request('POST', '/api/settings', '{}', {**auth, 'Origin': 'null'})[0], 403)
        self.assertEqual(self.request('POST', '/api/settings', '{}', {**auth, 'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request('POST', '/api/settings', '[]', auth)[0], 400)
        self.assertEqual(self.request('POST', '/api/settings', '{', auth)[0], 400)
        self.assertEqual(self.request('POST', '/api/settings', '', {**auth, 'Content-Length': str(bridge._MAX_JSON_BYTES + 1)})[0], 413)

    def test_untrusted_reads_and_vendor_assets(self):
        self.assertEqual(self.request('GET', '/api/chats', headers={'Origin': 'null'})[0], 403)
        self.assertEqual(self.request('GET', '/api/chats', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.request('GET', '/web/js/preview-runtime.js', headers={'Origin': 'null'})[0], 200)
        self.assertEqual(self.request('GET', '/assets/vendor/html2canvas/dist/html2canvas.min.js', headers={'Origin': 'null'})[0], 200)
        self.assertEqual(self.request('GET', '/assets/vendor/../../bridge.py')[0], 404)

    def test_saved_preview_has_sandbox_even_as_top_level_page(self):
        version = bridge.save_version('private-preview', '<!doctype html><HTML><HEAD lang="en"></HEAD><BODY>hello</BODY></HTML>')
        status, headers, body = self.request('GET', '/api/versions/private-preview/' + version['id'], headers={'Sec-Fetch-Dest': 'document'})
        self.assertEqual(status, 200)
        self.assertIn('sandbox allow-scripts', headers['Content-Security-Policy'])
        self.assertNotIn('allow-same-origin', headers['Content-Security-Policy'])
        self.assertIn(b'<HEAD lang="en"><script src="/web/js/preview-runtime.js">', body)

    def test_singleton_refuses_second_bind_without_startup(self):
        with bridge_patch('PORT', self.port), bridge_patch('_main_owned') as start:
            bridge.main()
            start.assert_not_called()
        self.assertEqual(self.request('GET', '/')[0], 200)

    def test_offline_status_does_not_fetch(self):
        with bridge_patch('_offline_active', True), patch('urllib.request.urlopen', side_effect=AssertionError('network request')):
            self.assertTrue(bridge.app_update_status()['offline'])
            self.assertEqual(self.request('GET', '/api/link_preview?url=https://example.com')[0], 409)
            status, headers, _ = self.request('GET', '/')
            self.assertEqual(status, 200)
            self.assertIn("connect-src 'self'", headers['Content-Security-Policy'])


class OfflineTests(unittest.TestCase):

    def setUp(self):
        sandbox_user_files(self)

    def test_socket_policy(self):
        for host in ['localhost', '127.0.0.1', '::1']:
            offline_socket_guard('socket.connect', (None, (host, 8080)))
        for host in ['example.com', '1.1.1.1', '192.168.1.2']:
            with self.assertRaises(OSError): offline_socket_guard('socket.connect', (None, (host, 443)))
            with self.assertRaises(OSError): offline_socket_guard('socket.getaddrinfo', (host, 443, 0, 0, 0))

    def test_vendor_checksums(self):
        manifest = json.loads((bridge.ROOT / 'assets/vendor/manifest.json').read_text())
        self.assertGreater(len(manifest), 20)
        for item in manifest:
            content = (bridge.ROOT / 'assets/vendor' / item['file']).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), item['sha256'], item['file'])


class ModelStartupTests(unittest.TestCase):

    def setUp(self):
        sandbox_user_files(self)

    def test_overlapping_model_load_is_refused(self):
        manager = type(bridge._llama)()
        entered, release = threading.Event(), threading.Event()
        def slow_start(**kwargs):
            entered.set()
            release.wait(3)
            return {'ok': True}
        with patch.object(manager, '_start_impl', side_effect=slow_start) as start:
            worker = threading.Thread(target=manager.start, args=('private-model',))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertFalse(manager.start('duplicate')['ok'])
                self.assertEqual(start.call_count, 1)
            finally:
                release.set()
                worker.join(3)
            self.assertTrue(manager.start('later')['ok'])
