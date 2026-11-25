import http.client
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch
from task_state import UndoStore


class RecordedChangesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = UndoStore(self.root / "undo")
        self.path = self.root / "example.txt"

    def change(self, before, after):
        if before is not None:
            self.path.write_bytes(before)
        self.store.begin("chat-a")
        self.store.snapshot(str(self.path))
        if after is None:
            self.path.unlink()
        else:
            self.path.write_bytes(after)
        self.store.checkpoint(str(self.path))
        return self.store.commit()

    def test_review_uses_checkpoint_even_when_file_changes_before_commit(self):
        self.path.write_text("original\n")
        self.store.begin("chat-a")
        self.store.snapshot(str(self.path))
        self.path.write_text("agent\n")
        self.store.checkpoint(str(self.path))
        self.path.write_text("human\n")
        result = self.store.commit()
        review = self.store.review(result["turn_id"], "chat-a", 0)
        self.assertIn("+agent", review["diff"])
        self.assertNotIn("+human", review["diff"])
        self.assertTrue(review["changed_since"])
        self.assertFalse(self.store.restore(result["turn_id"])["ok"])
        self.assertEqual(self.path.read_text(), "human\n")

    def test_selected_undo_preserves_other_files_and_allows_remaining_undo(self):
        other = self.root / "other.txt"
        self.path.write_text("original")
        other.write_text("other original")
        self.store.begin("chat-a")
        for path in (self.path, other):
            self.store.snapshot(str(path))
            path.write_text("agent edit")
            self.store.checkpoint(str(path))
        record = self.store.commit()
        result = self.store.restore(record["turn_id"], chat_id="chat-a", file_index=0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["remaining"], 1)
        self.assertEqual(self.path.read_text(), "original")
        self.assertEqual(other.read_text(), "agent edit")
        self.assertIn("error", self.store.restore(record["turn_id"], chat_id="chat-a", file_index=0))
        self.assertTrue(self.store.restore(record["turn_id"])["ok"])
        self.assertEqual(other.read_text(), "other original")

    def test_selected_undo_rejects_wrong_chat_and_preserves_later_edits(self):
        record = self.change(b"original", b"agent edit")
        for chat, index in (("wrong", 0), ("chat-a", -1), ("chat-a", True), ("chat-a", 8)):
            self.assertIn("error", self.store.restore(record["turn_id"], chat_id=chat, file_index=index))
        self.path.write_text("human edit")
        result = self.store.restore(record["turn_id"], chat_id="chat-a", file_index=0)
        self.assertFalse(result["ok"])
        self.assertEqual(self.path.read_text(), "human edit")

    def test_review_survives_restart_and_undo(self):
        result = self.change(b"before\r\n", b"after\n")
        reopened = UndoStore(self.root / "undo")
        self.assertTrue(reopened.restore(result["turn_id"])["ok"])
        review = reopened.review(result["turn_id"], "chat-a", 0)
        self.assertIn("+after", review["diff"])
        self.assertEqual(self.path.read_bytes(), b"before\r\n")

    def test_binary_metadata_does_not_offer_text_undo(self):
        result = self.change(b"\x00old", b"\x00new data")
        self.assertFalse(result["files"][0]["text_diff"])
        self.assertFalse(result["files"][0]["restorable"])
        review = self.store.review(result["turn_id"], "chat-a", 0)
        self.assertNotIn("diff", review)
        self.assertEqual(review["after_size"], 9)
        self.store.restore(result["turn_id"])
        self.assertEqual(self.path.read_bytes(), b"\x00new data")

    def test_created_and_deleted_files(self):
        created = self.change(None, b"created\n")
        self.assertTrue(created["files"][0]["created"])
        self.assertIn("+created", self.store.review(created["turn_id"], "chat-a", 0)["diff"])
        deleted = self.change(b"created\n", None)
        self.assertTrue(deleted["files"][0]["removed"])
        self.assertIn("-created", self.store.review(deleted["turn_id"], "chat-a", 0)["diff"])

    def test_lookup_rejects_wrong_chat_and_invalid_indices(self):
        result = self.change(b"a", b"b")
        for identifier, chat, index in [("../bad", "chat-a", 0), (result["turn_id"], "chat-b", 0),
                                        (result["turn_id"], "chat-a", -1),
                                        (result["turn_id"], "chat-a", True)]:
            self.assertIn("error", self.store.review(identifier, chat, index))
        self.assertIn("error", self.store.review("a" * 32, "chat-a"))

    def test_index_does_not_include_file_contents(self):
        result = self.change(b"private before", b"private after")
        index = self.store.review(result["turn_id"], "chat-a")
        self.assertNotIn("private", str(index))
        self.assertNotIn("before", index["files"][0])

    def test_oversized_files_have_metadata_without_text_diff(self):
        result = self.change(b"old", b"x" * (2 * 1024 * 1024 + 1))
        review = self.store.review(result["turn_id"], "chat-a", 0)
        self.assertNotIn("diff", review)
        self.assertIn("oversized", review["notice"])

    def test_review_endpoint_requires_authentication_and_matching_chat(self):
        result = self.change(b"before", b"after")
        class QuietHandler(bridge.Handler):
            def log_message(self, *args):
                pass
        server = bridge.AccurettaHTTPServer(("127.0.0.1", 0), QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(method, path, body=None, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request(method, path, body, headers or {})
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()
        try:
            with bridge_patch("_undo_store", self.store):
                _, page = request("GET", "/")
                token = re.search(rb'name="accuretta-request-token" content="([^"]+)"', page)[1].decode()
                payload = {"turn_id": result["turn_id"], "chat_id": "chat-a", "file_index": 0}
                self.assertEqual(request("POST", "/api/task-review", json.dumps(payload))[0], 403)
                headers = {"X-Accuretta-Token": token, "Content-Type": "application/json"}
                status, body = request("POST", "/api/task-review", json.dumps(payload), headers)
                self.assertEqual(status, 200)
                self.assertIn("+after", json.loads(body)["diff"])
                payload["chat_id"] = "wrong-chat"
                self.assertEqual(request("POST", "/api/task-review", json.dumps(payload), headers)[0], 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class TaskStatusTests(unittest.TestCase):
    def test_approval_takes_priority_over_running(self):
        with bridge_patch("list_approvals", return_value=[{"chat_id":"a", "status":"pending"}]), bridge_patch("_chat_emitters", {"a": object()}):
            self.assertEqual(bridge._chat_task_state("a", {}), "needs-you")
            self.assertEqual(bridge._chat_task_state("b", {}), "ready")

    def test_recovery_does_not_outlive_a_new_completed_reply(self):
        with bridge_patch("list_approvals", return_value=[]), bridge_patch("_chat_emitters", {}):
            chat = {"turn_recovery":{"recovered_at":10}, "messages":[{"role":"assistant", "t":5}]}
            self.assertEqual(bridge._chat_task_state("a", chat), "interrupted")
            chat["messages"].append({"role":"assistant", "t":20})
            self.assertEqual(bridge._chat_task_state("a", chat), "finished")
            chat["messages"][-1]["_failed"] = True
            self.assertEqual(bridge._chat_task_state("a", chat), "failed")


class TaskVerificationTests(unittest.TestCase):
    def setUp(self):
        self.files = [{"path": "project/file.py", "after_hash": "final-revision"}]
        self.key = bridge._verification_key(self.files[0]["path"])

    def check(self, name, proof=None, **result):
        return bridge._task_check_record(name, {"path": "project/file.py"},
                                         {"verification": proof or {}, **result})

    def test_no_checks_and_syntax_only_are_not_fully_verified(self):
        self.assertEqual(bridge._task_verification_summary(self.files, [])["status"], "unchecked")
        check = self.check("check_syntax", {"sha256": "final-revision"})
        self.assertEqual(bridge._task_verification_summary(self.files, [check])["status"], "partial")

    def test_syntax_files_counts_final_revisions_only(self):
        # The task card shows "Syntax OK" when every file's final revision passed.
        self.assertEqual(bridge._task_verification_summary(self.files, [])["syntax_files"], 0)
        check = self.check("check_syntax", {"sha256": "final-revision"})
        self.assertEqual(bridge._task_verification_summary(self.files, [check])["syntax_files"], 1)
        stale = self.check("check_syntax", {"sha256": "older-revision"})
        self.assertEqual(bridge._task_verification_summary(self.files, [stale])["syntax_files"], 0)

    def test_only_full_tests_on_matching_revisions_pass(self):
        proof = {"target": "host", "tests_ran": True, "revisions": {self.key: "final-revision"}}
        check = self.check("run_tests", proof)
        self.assertEqual(bridge._task_verification_summary(self.files, [check])["status"], "passed")
        proof["revisions"][self.key] = "older-revision"
        self.assertEqual(bridge._task_verification_summary(self.files, [check])["status"], "partial")

    def test_failures_are_visible_and_proofs_stay_out_of_summary(self):
        failed = self.check("run_tests", error="Tests failed", exit_code=1)
        summary = bridge._task_verification_summary(self.files, [failed])
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["checks"][0]["detail"], "Tests failed")
        self.assertNotIn("revisions", summary["checks"][0])

    def test_successful_rerun_resolves_failure_but_keeps_check_history(self):
        failed = self.check("run_tests", error="Tests failed", exit_code=1)
        passed = self.check("run_tests", {"target": "host", "tests_ran": True,
                                          "revisions": {self.key: "final-revision"}})
        summary = bridge._task_verification_summary(self.files, [failed, passed])
        self.assertEqual(summary["status"], "passed")
        self.assertEqual(len(summary["checks"]), 2)


if __name__ == "__main__":
    unittest.main()
