"""sql_injection against a page like real shops: three columns, only the name is
shown, and the price is formatted as a number (a NULL there makes a 500)."""
import sqlite3
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import bridge
from _patching import bridge_patch

SECRET = "flag{union-reaches-the-shown-column}"


def _make_db():
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.executescript("""
        CREATE TABLE products (id INTEGER, name TEXT, price REAL);
        INSERT INTO products VALUES (1, 'widget', 9.5), (2, 'gadget', 12.0);
        CREATE TABLE secret_notes (id INTEGER, note TEXT);
        INSERT INTO secret_notes VALUES (7, '%s');
    """ % SECRET)
    return db


class _Shop(BaseHTTPRequestHandler):
    db = None

    def log_message(self, *a):
        pass

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("q", [""])[0]
        try:
            rows = self.db.execute(
                f"SELECT id, name, price FROM products WHERE name LIKE '%{q}%'").fetchall()
            body = "".join(f"<li>{r[1]} — {r[2]:.2f}</li>" for r in rows)
            code = 200
        except sqlite3.Error as e:
            body, code = f"<pre>SQL error: {e}</pre>", 500
        except Exception as e:  # noqa: BLE001  (the page's own formatting error)
            body, code = f"<pre>{e}</pre>", 500
        data = f"<html><body><ul>{body}</ul></body></html>".encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class SqlInjectionToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _Shop.db = _make_db()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Shop)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}/search?q=widget"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def run_tool(self, **args):
        with bridge_patch("get_settings", return_value={"red_team_enabled": True}):
            return bridge.tool_sql_injection({"url": self.url, **args})

    def test_finds_columns_when_null_breaks_the_page(self):
        r = self.run_tool()
        self.assertTrue(r["injectable"])
        self.assertEqual(r["union_columns"], 3)
        self.assertEqual(r["reflected_columns"], [2])

    def test_extracts_into_the_shown_column(self):
        r = self.run_tool(extract="note FROM secret_notes")
        self.assertEqual(r["dump_status"], 200)
        self.assertIn(SECRET, r["dump_body"])

    def test_several_values_join_into_the_one_shown_column(self):
        r = self.run_tool(extract="id, note FROM secret_notes")
        self.assertIn(f"7~{SECRET}", r["dump_body"])
        self.assertIn("joined", r["note"])

    def test_select_list_split_keeps_function_commas_and_strings(self):
        self.assertEqual(bridge._split_sql_select_list("a, group_concat(b, ','), 'x,y'"),
                         ["a", "group_concat(b, ',')", "'x,y'"])


if __name__ == "__main__":
    unittest.main()
