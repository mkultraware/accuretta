"""The frontend secret scanner finds live keys, keeps public ones apart, and
shows only a shortened preview outside its internal value field.

It had no test of its own; this one guards it now that it lives in
accuretta.redteam.frontend_secrets. All values here are fake.
"""
import json
import unittest

import bridge

FAKE_STRIPE_LIVE = "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
FAKE_AWS_KEY_ID = "AKIA" + "QWERTYUIOPASDFGH"


class FrontendSecretScanTests(unittest.TestCase):
    def scan(self, text):
        return bridge._scan_frontend_source("https://example.test/app.js", text)

    def test_live_secret_key_is_a_critical_finding(self):
        found, _candidates, _public = self.scan(f'const s = "{FAKE_STRIPE_LIVE}";')
        self.assertEqual(len(found), 1)
        self.assertIn("Stripe", found[0]["type"])
        self.assertEqual(found[0]["severity"], "critical")

    def test_preview_is_shortened(self):
        # The full value travels only in the internal `_value` field, which the
        # red-team report and the credential guard read on purpose.
        found, _candidates, _public = self.scan(f'const s = "{FAKE_STRIPE_LIVE}";')
        self.assertNotIn(FAKE_STRIPE_LIVE, found[0]["preview"])
        self.assertEqual(found[0]["_value"], FAKE_STRIPE_LIVE)
        self.assertNotIn(FAKE_STRIPE_LIVE, json.dumps({k: v for k, v in found[0].items() if k != "_value"}))

    def test_aws_key_id_alone_is_listed_as_public(self):
        found, _candidates, public = self.scan(f'const k = "{FAKE_AWS_KEY_ID}";')
        self.assertEqual(found, [])
        self.assertTrue(any("AWS" in str(p.get("type")) for p in public))

    def test_plain_code_finds_nothing(self):
        found, candidates, public = self.scan("function add(a, b) { return a + b; }")
        self.assertEqual((found, candidates, public), ([], [], []))


if __name__ == "__main__":
    unittest.main()
