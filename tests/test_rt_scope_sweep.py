"""Engagement guard and model-guidance checks.

Covers three things the red-team suite promises the user:
  * a tool cannot be aimed outside the allowlist through a field the guard
    does not read (lists, a second target field, a network suffix);
  * "non-destructive only" and the request budget apply to every tool that
    sends a request, not just http_request;
  * the model is given one consistent set of instructions, an ordered route,
    and a short way to confirm a finding.
"""

import json
import unittest
from unittest.mock import patch

import bridge
from _patching import bridge_patch


def _mission_chat(engagement="pentest", **panel):
    chat = {"id": "c1"}
    base = {"authorized": True, "target": "example.com", "engagement": engagement,
            "scope_in": ["example.com", "203.0.113.5"]}
    base.update(panel)
    assert bridge._rt_mission_apply_panel(chat, base)
    return chat


class _Engagement(unittest.TestCase):
    engagement = "pentest"
    panel: dict = {}

    def setUp(self):
        self.chat = _mission_chat(self.engagement, **self.panel)
        settings = dict(bridge.get_settings(), red_team_enabled=True)
        for target, value in (("get_settings", lambda: settings),
                              ("_rt_context_chat", lambda: self.chat)):
            patcher = bridge_patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        bridge._RT_GOV_STATE.clear()

    def blocked(self, name, args):
        return bridge._rt_scope_block(name, args)


class ScopeArgumentTests(_Engagement):
    def test_in_scope_target_still_passes(self):
        self.assertIsNone(self.blocked("http_request", {"url": "https://example.com/a"}))
        self.assertIsNone(self.blocked("tcp_send", {"host": "api.example.com", "port": 22}))

    def test_every_target_field_is_checked(self):
        # The tool connects to `host`; the guard used to read only `target`.
        for tool in ("tcp_send", "recon_port_scan", "recon_tls_audit", "recon_open_services"):
            self.assertIn("victim.org", self.blocked(
                tool, {"target": "example.com", "host": "victim.org", "port": 22}) or "", tool)

    def test_lists_and_secondary_urls_are_checked(self):
        self.assertTrue(self.blocked("batch_probe", {"targets": ["https://victim.org/?a=1"]}))
        self.assertTrue(self.blocked("batch_probe", {
            "targets": ["https://example.com/?a=1", "https://victim.org/?a=1"]}))
        self.assertTrue(self.blocked("recon_subdomain_takeover", {"subdomains": ["a.victim.org"]}))
        self.assertTrue(self.blocked("recon_map", {
            "url": "https://example.com", "openapi_url": "https://victim.org/openapi.json"}))
        # A site-relative spec path belongs to the checked base URL.
        self.assertIsNone(self.blocked("recon_map", {
            "url": "https://example.com", "openapi_url": "/openapi.json"}))

    def test_network_suffix_cannot_widen_scope(self):
        self.assertTrue(self.blocked("sandbox_nmap", {"target": "203.0.113.5/16"}))
        self.assertTrue(self.blocked("sandbox_nmap", {"target": "example.com/16"}))
        self.assertIsNone(self.blocked("sandbox_nmap", {"target": "203.0.113.5"}))
        self.assertIsNone(self.blocked("sandbox_nmap", {"target": "203.0.113.5/32"}))

    def test_authorized_range_is_allowed_whole_or_in_part(self):
        self.chat = _mission_chat(scope_in=["10.0.0.0/24"], scope_out=["10.0.0.7"], target="10.0.0.1")
        self.assertIsNone(self.blocked("sandbox_nmap", {"target": "10.0.0.128/25"}))
        self.assertTrue(self.blocked("sandbox_nmap", {"target": "10.0.0.0/16"}))
        self.assertIn("10.0.0.7", self.blocked("sandbox_nmap", {"target": "10.0.0.0/24"}))

    def test_shared_fetch_refuses_out_of_scope_urls(self):
        with patch.object(bridge.urllib.request, "build_opener") as opener:
            result = bridge._recon_fetch("https://victim.org/x")
            self.assertTrue(result.get("scope_blocked"))
            opener.assert_not_called()

    def test_batch_probe_sends_nothing_out_of_scope(self):
        with patch.object(bridge.urllib.request, "build_opener") as opener:
            bridge.tool_batch_probe({"targets": ["https://victim.org/?a=1"], "probe": "xss"})
            opener.assert_not_called()

    def test_raw_tcp_refuses_out_of_scope_host(self):
        with patch.object(bridge.socket, "socket") as sock:
            result = bridge.tool_tcp_send({"host": "victim.org", "port": 22, "data": "x"})
            self.assertTrue(result.get("scope_blocked"))
            sock.assert_not_called()

    def test_bucket_names_cannot_carry_a_hostname(self):
        fetched = []
        with bridge_patch("request_approval", lambda *a, **k: {"decision": "approve"}), \
                bridge_patch("_recon_fetch",
                             lambda url, **k: fetched.append(url) or {"status": 404, "body": ""}):
            bridge._recon_check_s3("example.com", ["good-bucket", "victim.org/x?", "a@victim.org"])
        self.assertEqual(fetched, ["https://good-bucket.s3.amazonaws.com/?list-type=2"])

    def test_fetch_outside_an_engagement_is_untouched(self):
        self.chat = {"id": "c1"}
        self.assertIsNone(bridge._rt_connect_block("https://anything.example/"))


class NonDestructiveTests(_Engagement):
    panel = {"allow_destructive": False}

    def test_delete_and_its_overrides_are_refused(self):
        url = "https://example.com/api/users/5"
        self.assertTrue(self.blocked("http_request", {"url": url, "method": "DELETE"}))
        self.assertTrue(self.blocked("http_request", {
            "url": url, "method": "POST", "headers": {"X-HTTP-Method-Override": "DELETE"}}))
        self.assertTrue(self.blocked("http_request", {
            "url": url, "method": "POST", "body": "_method=DELETE&id=5"}))

    def test_destructive_urls_are_refused_on_every_request_tool(self):
        for tool in ("http_request", "fuzz", "rt_browser", "sql_injection", "sandbox_sqlmap"):
            self.assertTrue(self.blocked(tool, {"url": "https://example.com/account/delete"}), tool)
        self.assertTrue(self.blocked("http_request", {
            "url": "https://example.com/api/users/5/remove", "method": "POST"}))
        self.assertTrue(self.blocked("http_request", {"url": "https://example.com/admin?action=delete&id=5"}))
        self.assertTrue(self.blocked("batch_probe", {"targets": ["https://example.com/wipe?a=1"]}))

    def test_ordinary_requests_still_work(self):
        self.assertIsNone(self.blocked("http_request", {
            "url": "https://example.com/api/users/5", "method": "PUT", "body": "{}"}))
        self.assertIsNone(self.blocked("http_request", {"url": "https://example.com/search?q=delete"}))
        self.assertIsNone(self.blocked("http_request", {"url": "https://example.com/css/dropdown.css"}))

    def test_datastore_wipe_commands_are_refused(self):
        for payload in ("FLUSHALL\r\n", "*1\r\n$8\r\nFLUSHALL\r\n", "DROP TABLE users;",
                        "DELETE /api/users/5 HTTP/1.1\r\nHost: example.com\r\n\r\n"):
            self.assertTrue(self.blocked("tcp_send", {
                "host": "example.com", "port": 6379, "data": payload}), payload)
        self.assertIsNone(self.blocked("tcp_send", {"host": "example.com", "port": 6379, "data": "PING\r\n"}))

    def test_browser_refuses_delete_requests(self):
        policy = bridge._rt_browser_policy(self.chat)
        self.assertIs(policy["allow_destructive"], False)

    def test_allowed_when_the_user_ticked_the_box(self):
        self.chat = _mission_chat(allow_destructive=True)
        self.assertIsNone(self.blocked("http_request", {
            "url": "https://example.com/api/users/5", "method": "DELETE"}))


class RequestBudgetTests(_Engagement):
    panel = {"rate_limit_rpm": 3}

    def setUp(self):
        super().setUp()
        # Paced requests sleep until their slot; record the slots instead.
        self.slots = []
        sleeper = bridge_patch("_rt_gov_sleep_until",
                               side_effect=lambda when: self.slots.append(when) or True)
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_raw_tcp_is_paced_by_the_limit(self):
        with patch.object(bridge.socket, "socket") as sock:
            sock.return_value.recv.return_value = b""
            results = [bridge.tool_tcp_send({"host": "example.com", "port": 80, "data": "x",
                                             "read_timeout": 0.5}) for _ in range(4)]
        self.assertFalse(any(r.get("rate_limited") for r in results))
        self.assertEqual(len(self.slots), 4)
        self.assertGreaterEqual(self.slots[3] - self.slots[2], 20.0 - 1e-6)   # 3/min

    def test_port_scan_too_long_at_the_pace_is_refused_up_front(self):
        with bridge_patch("_recon_resolve", lambda host: {"ips": ["203.0.113.5"]}), \
                patch.object(bridge.socket, "socket") as sock:
            result = bridge.tool_recon_port_scan({"host": "example.com", "ports": list(range(1, 200))})
            self.assertTrue(result.get("rate_limited"))
            sock.assert_not_called()

    def test_login_spray_larger_than_the_budget_is_refused_up_front(self):
        with bridge_patch("_RT_GOV_PREFLIGHT_MAX_MIN", 0.01), \
                patch.object(bridge.urllib.request, "build_opener") as opener:
            result = bridge.tool_recon_auth_spray({"url": "https://example.com/login"})
            self.assertTrue(result.get("rate_limited"))
            opener.assert_not_called()

    def test_scanners_inherit_the_limit(self):
        commands = []
        with bridge_patch("request_approval", lambda **k: {"decision": "deny", "status": "x"}), \
                bridge_patch("_emit_execution_event", lambda kind, where, cmd: commands.append(cmd)), \
                bridge_patch("_execution_not_run", lambda *a, **k: {}):
            bridge.tool_sandbox_nmap({"target": "example.com"})
            bridge.tool_sandbox_sqlmap({"url": "https://example.com/?id=1"})
        self.assertIn("--max-rate 0.05", commands[0])
        self.assertIn("--delay=20.00", commands[1])


class GuidanceTests(_Engagement):
    def test_red_team_mode_is_not_told_to_hold_back(self):
        with bridge_patch("get_chats", lambda: {"chats": {"c1": self.chat}}), \
                bridge_patch("_get_current_chat", lambda: "c1"):
            prompt = bridge.build_system_prompt(True)
        self.assertIn("red team mode", prompt)
        self.assertNotIn("do not run injection probes", prompt)

    def test_recon_mode_keeps_the_hold_back_rule(self):
        self.chat = _mission_chat("recon")
        with bridge_patch("get_chats", lambda: {"chats": {"c1": self.chat}}), \
                bridge_patch("_get_current_chat", lambda: "c1"):
            prompt = bridge.build_system_prompt(True)
        self.assertIn("target recon mode", prompt)
        self.assertNotIn("red team mode", prompt)

    def test_mission_block_carries_the_ordered_steps(self):
        text = bridge._rt_mission_render(self.chat)
        self.assertIn("1. [NEXT]", text)
        self.assertIn("recon_map", text)
        self.assertLess(text.index("recon_map"), text.index("batch_probe"))
        self.assertLess(len(text), 2400)

    def test_black_box_by_default(self):
        """A bench run read the local target's source off the disk after 50
        wrong guesses; the engagement now says to test through the address."""
        self.assertIn("test the target only through its address", bridge._rt_mission_render(self.chat))
        osint = _mission_chat("passive_osint")
        self.assertNotIn("test the target only through its address", bridge._rt_mission_render(osint))

    def test_progress_follows_the_tools_that_ran(self):
        self.assertTrue(bridge._rt_mission_mark_tool(self.chat, "recon_map"))
        self.assertFalse(bridge._rt_mission_mark_tool(self.chat, "recon_map"))
        text = bridge._rt_mission_render(self.chat)
        self.assertIn("1. [started]", text)
        self.assertIn("2. [NEXT]", text)

    def test_steps_never_name_a_tool_the_engagement_switched_off(self):
        self.chat = _mission_chat(automated_allowed=False)
        text = bridge._rt_mission_render(self.chat)
        for tool in ("batch_probe", "recon_map", "fuzz"):
            self.assertNotIn(tool, text)

    def test_next_step_hints_match_the_mode(self):
        self.assertIn("batch_probe", bridge._rt_next_step("recon_map", {}, True))
        self.assertNotIn("batch_probe", bridge._rt_next_step("recon_map", {}, False))
        self.assertEqual(bridge._rt_next_step("recon_map", {"error": "x"}, True), "")
        self.assertIn("control_id", bridge._rt_next_step("record_finding", {"downgraded": "x"}, True))

    def test_any_destination_name_reaches_the_tool(self):
        unify = bridge._rt_unify_target_args
        self.assertEqual(unify("sandbox_nmap", {"host": "example.com"}), {"target": "example.com"})
        self.assertEqual(unify("recon_capture_evidence", {"target": "https://example.com/x"}),
                         {"url": "https://example.com/x"})
        self.assertEqual(unify("recon_tls_audit", {"url": "https://example.com:8443/x"}),
                         {"host": "example.com", "port": 8443})
        untouched = {"host": "example.com", "target": "other.example.com"}
        self.assertEqual(unify("recon_port_scan", dict(untouched)), untouched)

    def test_mission_tool_list_drops_coding_helpers(self):
        with bridge_patch("get_chats", lambda: {"chats": {"c1": self.chat}}), \
                bridge_patch("_get_current_chat", lambda: "c1"):
            names = {t["function"]["name"] for t in bridge.tools_for_llama(True, "c1")}
        self.assertFalse(names & {"check_syntax", "project_map", "save_skill", "review_finding"})
        self.assertTrue({"record_finding", "http_request", "recon_map", "read_file"} <= names)


class FindingShorthandTests(_Engagement):
    def _receipt(self, call_id, url, body):
        store, cid, mission_id = bridge._evidence_context()
        return store.record(cid, mission_id, call_id, "http_request", {"url": url},
                            {"status": 200, "body": body, "headers": {}})["id"]

    def setUp(self):
        super().setUp()
        patcher = bridge_patch("_get_current_chat", lambda: "c1")
        patcher.start()
        self.addCleanup(patcher.stop)
        url = "https://example.com/item?id=2"
        self.attack = [self._receipt("a", url, "owner: alice secret-note-77"),
                       self._receipt("b", url, "owner: alice secret-note-77")]
        self.control = self._receipt("c", "https://example.com/item?id=1", "owner: me my-own-note")

    def _flat(self, **extra):
        return {"title": "IDOR on item", "severity": "high", "evidence_ids": self.attack,
                "control_id": self.control, "proof": "secret-note-77", "parameter": "id",
                "expected_boundary": "Only the owner may read an item.",
                "demonstrated_impact": "Another user's note was returned.",
                "alternative_explanation": "The item could be public.",
                "disproof_check": "My own item returns different private text.",
                "limitations": "Only read access was shown.", **extra}

    def test_flat_fields_pass_the_full_review(self):
        review = bridge.tool_review_finding(self._flat())
        self.assertTrue(review["eligible"], json.dumps(review["issues"]))

    def test_receipts_fill_in_the_mechanical_fields(self):
        expanded = bridge._finding_expand_shorthand(self._flat())
        self.assertEqual(expanded["target"], "https://example.com/item?id=2")
        self.assertEqual(expanded["source_tool"], "http_request")
        self.assertEqual(expanded["challenge"]["test"]["method"], "GET")
        self.assertEqual(expanded["challenge"]["test"]["location"], "query")

    def test_review_rules_are_not_weakened(self):
        # Proof text that is also in the control must still fail.
        review = bridge.tool_review_finding(self._flat(proof="owner: "))
        self.assertFalse(review["eligible"])
        # A missing explanation must still fail.
        args = self._flat()
        del args["limitations"]
        self.assertFalse(bridge.tool_review_finding(args)["eligible"])

    def test_an_explicit_challenge_is_left_alone(self):
        given = {"title": "x", "target": "https://example.com/", "evidence_ids": self.attack,
                 "challenge": {"test": {"method": "POST"}}}
        self.assertEqual(bridge._finding_expand_shorthand(given), given)


if __name__ == "__main__":
    unittest.main()
