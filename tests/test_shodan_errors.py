"""A Shodan refusal says what it means (paid tier, bad key, rate limit), in
Accuretta's own Shodan tool and in results from third-party MCP servers, so
nobody chases a key or query problem that isn't there."""
import io
import unittest
import urllib.error
from unittest.mock import patch

import bridge


def _refusal(code: int, body: bytes):
    def raise_it(*_a, **_k):
        raise urllib.error.HTTPError("https://api.shodan.io/x", code, "Forbidden", {}, io.BytesIO(body))
    return raise_it


class BuiltInShodanTests(unittest.TestCase):
    def test_a_paid_only_call_says_so_with_shodans_reason(self):
        body = b'{"error": "Requires membership or higher to access"}'
        with patch.object(bridge.urllib.request, "urlopen", side_effect=_refusal(403, body)):
            data, err = bridge._shodan_get_json("https://api.shodan.io/shodan/host/1.1.1.1?key=SECRET")
        self.assertIsNone(data)
        self.assertIn("paid Shodan membership", err)
        self.assertIn("Requires membership or higher to access", err)
        self.assertNotIn("SECRET", err)

    def test_a_bad_key_and_a_rate_limit_read_differently(self):
        with patch.object(bridge.urllib.request, "urlopen", side_effect=_refusal(401, b"not json")):
            _, err = bridge._shodan_get_json("https://api.shodan.io/x")
        self.assertIn("rejected the API key", err)
        with patch.object(bridge.urllib.request, "urlopen", side_effect=_refusal(429, b"{}")):
            _, err = bridge._shodan_get_json("https://api.shodan.io/x")
        self.assertIn("out of query credits", err)

    def test_no_record_is_not_an_error(self):
        with patch.object(bridge.urllib.request, "urlopen", side_effect=_refusal(404, b"{}")):
            self.assertEqual(bridge._shodan_get_json("https://api.shodan.io/x"), (None, None))


class McpShodanTests(unittest.TestCase):
    def test_a_third_party_refusal_gets_explained(self):
        for text in ("Error: Shodan search failed: 403 Forbidden",
                     '{"error":"Shodan DNS resolve failed: 403"}'):
            with self.subTest(text=text):
                out = bridge._mcp_explain_refusal(text)
                self.assertTrue(out.startswith(text))
                self.assertIn("[Accuretta: Shodan refused this request (403)", out)

    def test_ordinary_results_are_left_alone(self):
        for text in ('{"source":"shodan","ports":[22,403,429]}', "VirusTotal lookup failed: 403", ""):
            with self.subTest(text=text):
                self.assertEqual(bridge._mcp_explain_refusal(text), text)


if __name__ == "__main__":
    unittest.main()
