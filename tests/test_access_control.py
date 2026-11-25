"""Access Control playbook: stored test accounts and the two-account replay.

What the playbook promises the user:
  * the sign-ins they type never reach the chat record, the model's context
    or a saved proof file, and are deleted when the engagement ends;
  * a request sent "as account A" really carries A's sign-in and nothing the
    model supplied, and stays inside the engagement's scope and budget;
  * rt_access_check tells a real leak apart from a public page, a shared
    page, a refusal and an expired sign-in, and never calls a candidate
    confirmed;
  * a repeated check gives record_finding everything it needs to validate.

The target is a small real HTTP server on loopback, so the requests go
through the same code path a live engagement uses.
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import bridge
from _patching import bridge_patch

ALICE = {"Cookie": "sid=alice-secret-cookie"}
BOB = {"Authorization": "Bearer bob-secret-token"}


def _who(handler) -> str:
    if "sid=alice-secret-cookie" in (handler.headers.get("Cookie") or ""):
        return "alice"
    if handler.headers.get("Authorization") == "Bearer bob-secret-token":
        return "bob"
    return ""


ALICE_ORDER = {"order": 1042, "email": "alice.owner@example.test", "address": "12 Harbour Lane",
               "items": ["blue kettle", "oak tray"], "total": "184.50"}
PROFILES = {"alice": {"name": "Alice Owner", "email": "alice.owner@example.test", "plan": "team"},
            "bob": {"name": "Bob Other", "email": "bob.other@example.test", "plan": "free"}}


class _Target(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *args):
        pass

    def _send(self, status, payload, content_type="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _route(self):
        who = _who(self)
        path = self.path.split("?")[0]
        type(self).seen.append((self.command, path, who, dict(self.headers)))
        denied = (401, {"error": "sign in required"})
        if path == "/public":
            return 200, {"motd": "welcome everyone to the public status page"}
        if path == "/gone":
            return 404, {"error": "not found"}
        if not who and path != "/api/strict/7":
            return denied
        if path == "/api/orders/1042":                 # no ownership check: the leak
            return 200, ALICE_ORDER
        if path == "/api/safe/1042":                   # ownership enforced
            return (200, ALICE_ORDER) if who == "alice" else (403, {"error": "forbidden"})
        if path == "/api/strict/7":                    # refuses others exactly like signed-out
            return (200, ALICE_ORDER) if who == "alice" else denied
        if path == "/api/me":
            return 200, PROFILES[who]
        if path == "/dashboard":
            return 200, {"widgets": ["news feed", "release notes", "help centre"]}
        if path == "/api/notes/5":
            return 200, {"saved": True}
        return 404, {"error": "not found"}

    def do_GET(self):
        self._send(*self._route())

    do_POST = do_PUT = do_HEAD = do_GET


class _AccessEngagement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Target)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def panel(self, **extra):
        panel = {"authorized": True, "target": "127.0.0.1", "engagement": "access_control",
                 "scope_in": ["127.0.0.1"], "identities": {"A": dict(ALICE), "B": dict(BOB)},
                 "owned_urls": [f"{self.base}/api/orders/1042"]}
        panel.update(extra)
        return panel

    def setUp(self):
        _Target.seen = []
        self.chat = {"id": "c1"}
        self.assertTrue(bridge._rt_mission_apply_panel(self.chat, self.panel()))
        settings = dict(bridge.get_settings(), red_team_enabled=True)
        real_save = bridge.save_json

        def save(path, value):
            if path != bridge.CHATS_FILE:      # never write the test chat to disk
                real_save(path, value)

        for target, value in (("get_settings", lambda: settings),
                              ("_rt_context_chat", lambda: self.chat),
                              ("get_chats", lambda: {"chats": {"c1": self.chat}}),
                              ("save_json", save),
                              ("_rt_proxy_handler", lambda: None)):
            patcher = bridge_patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        token = bridge._current_chat_id.set("c1")
        self.addCleanup(bridge._current_chat_id.reset, token)
        self.addCleanup(bridge._rt_identities_forget, self.chat.get("mission"))
        bridge._RT_GOV_STATE.clear()
        bridge._RT_IDENTITY_SEEN.clear()

    def check(self, path, **args):
        return bridge.tool_rt_access_check({"url": self.base + path, **args})

    def verdicts(self, result):
        return {row["account"]: row.get("verdict") for row in result["responses"]}


class StoredAccountTests(_AccessEngagement):
    def test_sign_ins_stay_out_of_the_chat_and_the_model_context(self):
        mission = self.chat["mission"]
        self.assertEqual(mission["identities"], ["A", "B"])
        everything_the_model_or_ui_sees = json.dumps(self.chat) + bridge._rt_mission_render(self.chat)
        for secret in ("alice-secret-cookie", "bob-secret-token"):
            self.assertNotIn(secret, everything_the_model_or_ui_sees)
        self.assertIn("test accounts: A, B", bridge._rt_mission_render(self.chat))
        self.assertIn("/api/orders/1042", bridge._rt_mission_render(self.chat))
        self.assertEqual(bridge._rt_active_identities(), {"A": ALICE, "B": BOB})

    def test_ending_the_engagement_deletes_the_sign_ins(self):
        key = self.chat["mission"]["authorization_id"]
        self.assertIn(key, bridge._rt_identities_read())
        bridge._rt_log_mission_ended(self.chat["mission"])
        self.assertNotIn(key, bridge._rt_identities_read())
        self.assertEqual(bridge._rt_active_identities(), {})

    def test_a_new_engagement_replaces_the_old_sign_ins(self):
        old = self.chat["mission"]["authorization_id"]
        self.assertTrue(bridge._rt_mission_apply_panel(
            self.chat, {"authorized": True, "target": "127.0.0.1", "engagement": "pentest"}))
        self.assertNotIn(old, bridge._rt_identities_read())
        self.assertNotIn("identities", self.chat["mission"])
        self.assertEqual(bridge._rt_active_identities(), {})

    def test_startup_sweep_drops_sign_ins_of_ended_engagements(self):
        bridge._rt_identities_save("gone-engagement", {"A": dict(ALICE), "B": dict(BOB)})
        self.assertEqual(bridge._rt_identities_sweep(), 1)
        stored = bridge._rt_identities_read()
        self.assertNotIn("gone-engagement", stored)
        self.assertIn(self.chat["mission"]["authorization_id"], stored)

    def test_startup_sweep_drops_sign_ins_of_an_engagement_left_open_too_long(self):
        key = self.chat["mission"]["authorization_id"]
        self.chat["mission"]["authorized_at"] -= bridge._RT_IDENTITY_KEEP_SECONDS + 60
        self.assertEqual(bridge._rt_identities_sweep(), 1)
        self.assertNotIn(key, bridge._rt_identities_read())
        result = bridge.tool_rt_access_check({"url": self.base + "/api/me"})
        self.assertTrue(result.get("not_executed"))

    def test_the_playbook_needs_two_different_accounts(self):
        for identities in ({}, {"A": dict(ALICE)}, {"A": dict(ALICE), "B": dict(ALICE)},
                           {"A": dict(ALICE), "B": {"cookie": "sid=alice-secret-cookie"}},
                           {"A": dict(ALICE), "B": {"Host": "evil.example"}}, "A,B", None):
            with self.subTest(identities=identities):
                self.assertFalse(bridge._rt_identities_usable(identities))
                self.assertFalse(bridge._rt_mission_apply_panel(
                    {"id": "c2"}, self.panel(identities=identities)))
        self.assertTrue(bridge._rt_identities_usable({"A": ALICE, "B": BOB}))

    def test_sign_in_headers_are_cleaned(self):
        cleaned = bridge._rt_clean_identity_headers({
            "Cookie": " sid=1 ", "cookie": "dup", "Host": "evil.example", "User-Agent": "x",
            "Bad Name": "x", "X-Break": "a\r\nInjected: 1", "X-Long": "v" * 9000,
            "Authorization": "Bearer " + "t" * 4000, "X-Uni": "café ☃"})
        self.assertEqual(set(cleaned), {"Cookie", "Authorization"})
        self.assertEqual(cleaned["Cookie"], "sid=1")
        self.assertEqual(len(cleaned["Authorization"]), 4007)

    def test_owned_urls_are_cleaned(self):
        self.assertEqual(bridge._rt_clean_owned_urls(
            "https://a.test/x\n/api/me\nnot a url\n//evil.test/x\nhttps://a.test/x\njavascript:1"),
            ["https://a.test/x", "/api/me"])

    def test_account_names_are_read_forgivingly(self):
        for text, label in (("A", "A"), ("b", "B"), ("Account A", "A"), ("user_b", "B"),
                            ("test account a", "A"), ("anonymous", "anonymous"),
                            ("signed-out", "anonymous"), ("Logged Out", "anonymous"),
                            ("admin", ""), ("", ""), (None, ""), ("AB", ""), ("C", "")):
            with self.subTest(text=text):
                self.assertEqual(bridge._rt_identity_label(text), label)

    def test_other_engagements_carry_no_accounts(self):
        chat = {"id": "c3"}
        self.assertTrue(bridge._rt_mission_apply_panel(chat, self.panel(engagement="pentest")))
        self.assertNotIn("identities", chat["mission"])
        self.assertNotIn(chat["mission"]["authorization_id"], bridge._rt_identities_read())


class NamedAccountRequestTests(_AccessEngagement):
    def request(self, path="/api/me", **args):
        return bridge.tool_http_request({"url": self.base + path, **args})

    def test_the_bridge_attaches_the_named_accounts_sign_in(self):
        for label, name in (("A", "Alice Owner"), ("account b", "Bob Other")):
            result = self.request(identity=label)
            self.assertEqual(result["status"], 200, result)
            self.assertIn(name, result["body"])
            self.assertEqual(result["request_identity"], bridge._rt_identity_label(label))
        anonymous = self.request(identity="anonymous")
        self.assertEqual(anonymous["status"], 401)
        self.assertEqual(_Target.seen[-1][2], "")
        self.assertNotIn("Cookie", _Target.seen[-1][3])

    def test_the_result_never_echoes_the_sign_in(self):
        result = self.request(identity="A")
        self.assertNotIn("alice-secret-cookie", json.dumps(result))

    def test_fingerprint_separates_accounts_and_is_stable_per_account(self):
        first, again, other = (self.request(identity=x)["request_identity_fingerprint"]
                               for x in ("A", "A", "B"))
        anonymous = self.request(identity="anonymous")["request_identity_fingerprint"]
        self.assertEqual(first, again)
        self.assertEqual(len({first, other, anonymous}), 3)

    def test_identity_cannot_be_mixed_with_model_credentials(self):
        for extra in ({"cookies": {"sid": "x"}}, {"session": "s1"},
                      {"headers": {"Authorization": "Bearer x"}}, {"headers": {"cookie": "sid=x"}}):
            with self.subTest(extra=extra):
                before = len(_Target.seen)
                result = self.request(identity="anonymous", **extra)
                self.assertTrue(result.get("not_executed"), result)
                self.assertEqual(len(_Target.seen), before)

    def test_unknown_account_is_refused_without_sending(self):
        result = self.request(identity="admin")
        self.assertTrue(result.get("not_executed"))
        self.assertIn("A, B, anonymous", result["error"])
        self.assertEqual(_Target.seen, [])

    def test_identity_is_unavailable_outside_the_playbook(self):
        self.assertTrue(bridge._rt_mission_apply_panel(
            self.chat, {"authorized": True, "target": "127.0.0.1", "engagement": "pentest"}))
        result = self.request(identity="A")
        self.assertTrue(result.get("not_executed"))
        self.assertIn("Access Control playbook", result["error"])
        self.assertEqual(_Target.seen, [])

    def test_saved_proof_file_hides_the_sign_in(self):
        result = self.request(identity="A", save_evidence="proof")
        transcript = open(result["evidence_saved"], encoding="utf-8").read()
        self.assertNotIn("alice-secret-cookie", transcript)
        self.assertIn("[account A sign-in, held by Accuretta]", transcript)
        self.assertIn("Alice Owner", transcript)

    def test_plain_requests_are_unchanged(self):
        result = self.request(headers={"Cookie": "sid=alice-secret-cookie"})
        self.assertEqual(result["status"], 200)
        self.assertNotIn("request_identity", result)

    def test_sign_in_is_not_forwarded_to_another_host(self):
        handler = bridge._IdentityRedirect("http://127.0.0.1:1/x", ["Cookie", "X-Api-Key"])
        original = bridge.urllib.request.Request(
            "http://127.0.0.1:1/x", headers={"Cookie": "sid=1", "X-Api-Key": "k", "Accept": "*/*"})
        with bridge_patch("_rt_scope_block", lambda *a, **k: None):
            elsewhere = handler.redirect_request(original, None, 302, "Found", {}, "http://other.test/y")
            same_host = handler.redirect_request(original, None, 302, "Found", {}, "http://127.0.0.1:1/z")
        self.assertEqual({k.lower() for k in elsewhere.headers}, {"accept"})
        self.assertEqual({k.lower() for k in same_host.headers}, {"cookie", "x-api-key", "accept"})

    def test_map_crawls_signed_in_as_the_named_account(self):
        fetched = []

        def fetch(url, **kwargs):
            fetched.append(kwargs.get("extra_headers") or {})
            return {"status": 200, "headers": {}, "body": "", "url": url}

        with bridge_patch("_recon_fetch", fetch):
            signed_in = bridge.tool_recon_map({"url": self.base, "identity": "A", "auto_spec": False})
            signed_out = bridge.tool_recon_map({"url": self.base, "auto_spec": False})
        self.assertEqual(fetched[0], ALICE)
        self.assertEqual(fetched[-1], {})
        self.assertIn("rt_access_check", signed_in["next"])
        self.assertNotIn("batch_probe", signed_in["next"])
        self.assertIn("identity 'A'", signed_out["next"])


class AccessCheckTests(_AccessEngagement):
    def test_leak_is_a_candidate_with_quotable_proof(self):
        result = self.check("/api/orders/1042")
        self.assertEqual(result["outcome"], "candidate", result)
        self.assertEqual(self.verdicts(result),
                         {"A": "baseline", "B": "same_as_owner", "anonymous": "denied"})
        self.assertIn("alice.owner@example.test", result["proof_candidates"])
        self.assertIn("not a confirmed finding", result["summary"])
        self.assertTrue(result["confirm_with"]["evidence_id"])
        self.assertTrue(result["confirm_with"]["control_id"])
        self.assertEqual([who for _, _, who, _ in _Target.seen], ["alice", "bob", ""])
        for secret in ("alice-secret-cookie", "bob-secret-token"):
            self.assertNotIn(secret, json.dumps(result))

    def test_enforced_boundary(self):
        result = self.check("/api/safe/1042")
        self.assertEqual(result["outcome"], "enforced", result)
        self.assertEqual(self.verdicts(result)["B"], "denied")
        self.assertNotIn("proof_candidates", result)

    def test_each_account_seeing_its_own_data_is_not_a_leak(self):
        result = self.check("/api/me")
        self.assertEqual(result["outcome"], "inspect", result)
        self.assertEqual(self.verdicts(result)["B"], "different_content")

    def test_public_page_is_not_reported_as_a_leak(self):
        result = self.check("/public")
        self.assertEqual(result["outcome"], "open_without_sign_in", result)
        self.assertNotIn("confirm_with", result)

    def test_shared_page_without_an_object_id_is_only_a_conditional_candidate(self):
        result = self.check("/dashboard")
        self.assertEqual(result["outcome"], "candidate_if_restricted", result)
        self.assertIn("every signed-in account shares", result["summary"])

    def test_refusal_identical_to_signed_out_needs_a_working_sign_in_first(self):
        first = self.check("/api/strict/7")
        self.assertEqual(first["outcome"], "sign_in_unverified", first)
        self.assertEqual(first["signed_in_seen"], {"A": True, "B": False})
        self.assertIn("owner='B'", first["next_step"])
        own_page = self.check("/api/me", owner="B")       # B's sign-in is seen working
        self.assertTrue(own_page["signed_in_seen"]["B"])
        again = self.check("/api/strict/7")
        self.assertEqual(again["outcome"], "enforced", again)

    def test_owner_failure_stops_before_spending_more_requests(self):
        result = self.check("/gone")
        self.assertEqual(result["outcome"], "owner_could_not_read", result)
        self.assertEqual(result["requests_sent"], 1)
        self.assertEqual(len(_Target.seen), 1)
        self.assertEqual([row.get("not_sent") for row in result["responses"]], [None, True, True])

    def test_owner_can_be_the_other_account(self):
        result = self.check("/api/me", owner="account b")
        self.assertEqual(result["owner"], "B")
        self.assertEqual([who for _, _, who, _ in _Target.seen], ["bob", "alice", ""])

    def test_writes_need_confirmation_and_go_least_privileged_first(self):
        refused = self.check("/api/notes/5", method="POST", body="text=hello")
        self.assertTrue(refused.get("state_change_unconfirmed"))
        self.assertEqual(_Target.seen, [])
        result = self.check("/api/notes/5", method="POST", body="text=hello", confirm_state_change=True)
        self.assertEqual([(m, who) for m, _, who, _ in _Target.seen],
                         [("POST", ""), ("POST", "bob"), ("POST", "alice")])
        self.assertEqual(result["outcome"], "candidate", result)

    def test_credentials_from_the_model_are_refused(self):
        for extra in ({"headers": {"Cookie": "sid=x"}}, {"cookies": {"a": "b"}},
                      {"session": "s"}, {"identity": "A"}, {"owner": "admin"}):
            with self.subTest(extra=extra):
                result = self.check("/api/me", **extra)
                self.assertTrue(result.get("not_executed"), result)
        self.assertEqual(_Target.seen, [])

    def test_request_budget_ends_the_check_as_inconclusive(self):
        self.assertTrue(bridge._rt_mission_apply_panel(self.chat, self.panel(request_cap=2)))
        result = self.check("/api/orders/1042")
        self.assertEqual(result["outcome"], "inconclusive", result)
        self.assertEqual(len(_Target.seen), 2)

    def test_unavailable_without_stored_accounts(self):
        self.assertTrue(bridge._rt_mission_apply_panel(
            self.chat, {"authorized": True, "target": "127.0.0.1", "engagement": "pentest"}))
        result = self.check("/api/orders/1042")
        self.assertTrue(result.get("not_executed"))
        self.assertEqual(_Target.seen, [])

    def test_similarity_and_verdict_helpers(self):
        self.assertEqual(bridge._access_similarity("same text", "same text"), 1.0)
        self.assertEqual(bridge._access_similarity("", "anything here"), 0.0)
        self.assertLess(bridge._access_similarity(json.dumps(PROFILES["alice"]),
                                                  json.dumps(PROFILES["bob"])), bridge._ACCESS_SAME)
        page = "<html><h1>Order 1042</h1><p>alice.owner@example.test</p><input value='%s'></html>"
        self.assertGreaterEqual(bridge._access_similarity(page % "csrf-aaa", page % "csrf-bbb"),
                                bridge._ACCESS_SAME)
        owner = {"status": 200, "body": "x" * 50, "final_url": "http://t/a"}
        self.assertEqual(bridge._access_verdict({"status": 200, "body": "x" * 50,
                                                 "final_url": "http://t/login"}, owner, "http://t/a")[0],
                         "denied")
        self.assertEqual(bridge._access_verdict({"status": 200, "body": "x" * 50,
                                                 "final_url": "http://t/home"}, owner, "http://t/a")[0],
                         "redirected")
        self.assertEqual(bridge._access_verdict({"status": 503, "body": ""}, owner, "http://t/a")[0],
                         "inconclusive")
        self.assertEqual(bridge._access_verdict({"error": "timeout"}, owner, "http://t/a")[0],
                         "no_response")


class GuardAndRegistryTests(_AccessEngagement):
    def test_tool_is_registered_and_gated_on_stored_accounts(self):
        self.assertIn("rt_access_check", bridge.TOOLS)
        self.assertIn("rt_access_check", bridge._RT_EXPLOIT_TOOL_NAMES)
        self.assertIn("identity", bridge.TOOLS["http_request"]["parameters"]["properties"])
        self.assertTrue(bridge._rt_incl_exploit_for(self.chat))
        with bridge_patch("get_chats", lambda: {"chats": {"c1": self.chat}}):
            hidden = bridge._excluded_tools(True, "c1")
            self.assertNotIn("rt_access_check", hidden)
            self.assertNotIn("http_request", hidden)
            self.assertTrue(bridge._RT_ACCESS_HIDDEN_TOOLS <= hidden)
            self.assertTrue(bridge._rt_mission_apply_panel(
                self.chat, {"authorized": True, "target": "127.0.0.1", "engagement": "pentest"}))
            hidden = bridge._excluded_tools(True, "c1")
            self.assertIn("rt_access_check", hidden)
            self.assertNotIn("sql_injection", hidden)

    def test_scope_and_destructive_guards_cover_the_tool(self):
        self.assertIn("victim.org", bridge._rt_scope_block(
            "rt_access_check", {"url": "https://victim.org/api/orders/1"}) or "")
        self.assertIsNone(bridge._rt_scope_block("rt_access_check", {"url": self.base + "/api/me"}))
        self.assertIn("DELETE", bridge._rt_scope_block(
            "rt_access_check", {"url": self.base + "/api/me", "method": "DELETE"}) or "")
        self.assertTrue(bridge._rt_scope_block(
            "rt_access_check", {"url": self.base + "/api/orders/1/delete"}))

    def test_playbook_route_and_hints(self):
        lines = "\n".join(bridge._rt_playbook_lines(self.chat["mission"]))
        self.assertIn("rt_access_check", lines)
        self.assertIn("[NEXT]", lines)
        self.assertIn("rt_access_check", bridge._rt_next_step("recon_map", {}, True, "access_control"))
        self.assertEqual(bridge._rt_next_step("fuzz", {}, True, "access_control"), "")
        self.assertIn("batch_probe", bridge._rt_next_step("recon_map", {}, True, "pentest"))

    def test_running_through_the_dispatcher_records_receipts(self):
        result = bridge.invoke_tool("rt_access_check", {"url": self.base + "/api/orders/1042"})
        self.assertEqual(result.get("outcome"), "candidate", result)
        self.assertTrue(result["evidence_receipt"]["id"])
        store, cid, mission_id = bridge._evidence_context()
        for row in result["responses"]:
            record = store.read(cid, mission_id, row["evidence_id"])
            self.assertEqual(record["tool"], "rt_access_check")
            self.assertEqual(record["identity"], row["account"])
            self.assertNotIn("secret", json.dumps(record))
        blocked = bridge.invoke_tool("rt_access_check", {"url": "https://victim.org/api/orders/1"})
        self.assertTrue(blocked.get("scope_blocked"))


class ConfirmFindingTests(_AccessEngagement):
    def finding(self, first, second, **extra):
        leak = lambda run: next(r["evidence_id"] for r in run["responses"] if r["account"] == "B")
        args = {
            "title": "Order details readable by another account", "severity": "high",
            "status": "validated", "target": self.base + "/api/orders/1042",
            "evidence_ids": [leak(first), leak(second)],
            "control_id": first["confirm_with"]["control_id"],
            "proof": "alice.owner@example.test",
            "expected_boundary": "Only the account that placed an order may read it.",
            "demonstrated_impact": "Account B read account A's order, including email and address.",
            "alternative_explanation": "The order could be public or shared between both accounts.",
            "disproof_check": "A signed-out request to the same URL was refused with 401.",
            "limitations": "Only one order id was tested; write access was not tested.",
        }
        args.update(extra)
        return bridge.tool_record_finding(args)

    def run_check(self, call_id):
        token = bridge._current_tool_call_id.set(call_id)
        try:
            return self.check("/api/orders/1042")
        finally:
            bridge._current_tool_call_id.reset(token)

    def test_two_runs_validate_a_leak(self):
        first, second = self.run_check("call-1"), self.run_check("call-2")
        result = self.finding(first, second)
        self.assertEqual(result["finding"]["status"], "validated", result.get("downgraded"))
        self.assertTrue(result["finding"]["source_observed"])

    def test_naming_the_path_id_as_the_parameter_does_not_block_validation(self):
        # Seen in a live run: the model passed parameter="id" for /api/orders/1042.
        first, second = self.run_check("call-1"), self.run_check("call-2")
        result = self.finding(first, second, parameter="id", source_tool="rt_access_check")
        self.assertEqual(result["finding"]["status"], "validated", result.get("downgraded"))

    def test_one_run_is_not_enough(self):
        first = self.run_check("call-1")
        result = self.finding(first, first)
        self.assertEqual(result["finding"]["status"], "candidate")
        self.assertTrue(result["downgraded"])

    def test_proof_that_the_signed_out_response_also_contains_is_rejected(self):
        first, second = self.run_check("call-1"), self.run_check("call-2")
        # '": "' is JSON punctuation present in the leaked order and in the
        # signed-out error alike, so it proves nothing about the leak.
        result = self.finding(first, second, proof='": "')
        self.assertEqual(result["finding"]["status"], "candidate")
        self.assertIn("must fail on the control", result["downgraded"])

    def test_mixing_accounts_as_reproduction_is_rejected(self):
        first, second = self.run_check("call-1"), self.run_check("call-2")
        owner_receipt = next(r["evidence_id"] for r in second["responses"] if r["account"] == "A")
        leak_receipt = next(r["evidence_id"] for r in first["responses"] if r["account"] == "B")
        result = self.finding(first, second, evidence_ids=[leak_receipt, owner_receipt])
        self.assertEqual(result["finding"]["status"], "candidate")
        self.assertIn("same recorded identity", result["downgraded"])


if __name__ == "__main__":
    unittest.main()
