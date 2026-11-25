import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch


def _fp(name, identities=(), vocab=0, tokenizer="", embed=0, arch="", is_draft=None):
    if is_draft is None:
        is_draft = arch in {"dflash", "dspark"}
    return {
        "path": name,
        "name": Path(name).name,
        "size": 1,
        "ok": True,
        "architecture": arch,
        "vocab": vocab,
        "tokenizer": tokenizer,
        "embedding_length": embed,
        "block_count": 5,
        "identities": set(identities),
        "is_draft": is_draft,
    }


class DraftNameTests(unittest.TestCase):
    def test_draft_names_are_detected(self):
        for name in [
            "Qwen3.8-27B-DFlash2-Q4_K_M.gguf",
            "qwen-27b-dspark-q8.gguf",
            "model-spec-draft.gguf",
            "Qwen3.8-27B-Draft-Q4.gguf",
            "Qwen3.8-27B-DFlash2.gguf".upper(),
        ]:
            self.assertTrue(bridge._is_draft_name(name), name)

    def test_real_models_are_not_drafts(self):
        for name in [
            "Qwen3.8-27B-UD-Q4_K_M.gguf",
            "mmproj-F16.gguf",
            "drafting-model.gguf",
            "grandmaster.gguf",
            "",
        ]:
            self.assertFalse(bridge._is_draft_name(name), name)


class IdentityMatchTests(unittest.TestCase):
    def test_exact_identity_match_is_decisive(self):
        score, ids = bridge._identity_match({"qwen3827b"}, {"qwen3827bdflash2", "qwen3827b"})
        self.assertEqual(score, 100)
        self.assertEqual(ids, ["qwen3827b"])

    def test_containment_identity_match(self):
        score, ids = bridge._identity_match({"qwen3827bunleashed"}, {"qwen3827b"})
        self.assertEqual(score, 60)
        self.assertTrue(ids)

    def test_unrelated_identities_do_not_match(self):
        score, ids = bridge._identity_match({"ornith159b"}, {"qwen3827bdflash2"})
        self.assertEqual(score, 0)
        self.assertEqual(ids, [])


class DraftMatchTests(unittest.TestCase):
    TARGET = _fp("target.gguf", identities={"qwen3827b"}, vocab=248320,
                 tokenizer="gpt2", embed=5120, arch="qwen35")
    DRAFT = _fp("draft.gguf", identities={"qwen3827bdflash2", "qwen3827b"}, vocab=248320,
                tokenizer="gpt2", embed=5120, arch="dflash")

    def _match(self, target, draft):
        def fake(path):
            return target if "target" in str(path) else draft
        with bridge_patch("_spec_fingerprint", side_effect=fake):
            return bridge.spec_draft_match("target.gguf", "draft.gguf")

    def test_matching_pair_scores_high(self):
        result = self._match(self.TARGET, self.DRAFT)
        self.assertTrue(result["matched"])
        self.assertGreaterEqual(result["score"], 160)
        self.assertTrue(any("base model match" in r for r in result["reasons"]))

    def test_different_base_model_is_rejected(self):
        other = _fp("draft.gguf", identities={"ornith159b"}, vocab=248320,
                    tokenizer="gpt2", embed=5120, arch="dflash")
        result = self._match(self.TARGET, other)
        self.assertFalse(result["matched"])
        self.assertTrue(any("different base model" in r for r in result["reasons"]))

    def test_structural_match_without_identities(self):
        target = _fp("target.gguf", identities=set(), vocab=128000,
                     tokenizer="llama", embed=4096, arch="llama")
        draft = _fp("draft.gguf", identities=set(), vocab=128000,
                    tokenizer="llama", embed=4096, arch="dflash")
        self.assertTrue(self._match(target, draft)["matched"])

    def test_structural_mismatch_without_identities_is_rejected(self):
        target = _fp("target.gguf", identities=set(), vocab=128000,
                     tokenizer="llama", embed=4096, arch="llama")
        draft = _fp("draft.gguf", identities=set(), vocab=32000,
                    tokenizer="llama", embed=4096, arch="dflash")
        self.assertFalse(self._match(target, draft)["matched"])

    def test_non_draft_file_is_rejected(self):
        target = self.TARGET
        fake_draft = _fp("draft.gguf", identities={"qwen3827bdflash2", "qwen3827b"},
                         vocab=248320, tokenizer="gpt2", embed=5120, arch="qwen35",
                         is_draft=False)
        result = self._match(target, fake_draft)
        self.assertFalse(result["matched"])
        self.assertTrue(any("not a DFlash/DSpark draft" in r for r in result["reasons"]))


class FindDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sub = self.root / "models"
        self.sub.mkdir()
        self.target = self.sub / "target.gguf"
        self.draft = self.sub / "Qwen3.8-27B-DFlash2.gguf"
        for path in (self.target, self.draft):
            path.write_bytes(b"gguf")

    def _patch(self, target_fp, draft_fp):
        def fake(path):
            return target_fp if "target" in str(path) else draft_fp
        return bridge_patch("_spec_fingerprint", side_effect=fake)

    def test_validated_pair_is_found(self):
        target = _fp("target.gguf", identities={"qwen3827b"}, vocab=248320,
                     tokenizer="gpt2", embed=5120, arch="qwen35")
        draft = _fp("draft.gguf", identities={"qwen3827b"}, vocab=248320,
                    tokenizer="gpt2", embed=5120, arch="dflash")
        with bridge_patch("get_settings", return_value={"models_dir": str(self.sub)}), \
             self._patch(target, draft):
            found = bridge.find_spec_draft_for(str(self.target))
        self.assertEqual(found["path"], str(self.draft.resolve()))
        self.assertTrue(found["matched"])

    def test_mismatched_single_draft_is_not_auto_selected(self):
        target = _fp("target.gguf", identities={"ornith159b"}, vocab=248320,
                     tokenizer="gpt2", embed=5120, arch="ornith")
        draft = _fp("draft.gguf", identities={"qwen3827b"}, vocab=248320,
                    tokenizer="gpt2", embed=5120, arch="dflash")
        with bridge_patch("get_settings", return_value={"models_dir": str(self.sub)}), \
             self._patch(target, draft):
            self.assertEqual(bridge.find_spec_draft_for(str(self.target)), {})


class ScanPickerTests(unittest.TestCase):
    def test_drafts_hidden_from_picker_but_listable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "chat-model.gguf").write_bytes(b"gguf")
            (root / "Qwen3.8-27B-DFlash2.gguf").write_bytes(b"gguf")
            (root / "mmproj-F16.gguf").write_bytes(b"gguf")
            visible = [m["name"] for m in bridge.scan_gguf_dir(str(root))]
            with_drafts = [m["name"] for m in bridge.scan_gguf_dir(str(root), include_drafts=True)]
        self.assertEqual(visible, ["chat-model.gguf"])
        self.assertIn("Qwen3.8-27B-DFlash2.gguf", with_drafts)
        self.assertNotIn("mmproj-F16.gguf", with_drafts)


class TuneReserveTests(unittest.TestCase):
    """auto_tune must reserve the draft's VRAM, and only for DFlash/DSpark."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.draft = Path(self.tmp.name) / "draft.gguf"
        self.draft.write_bytes(b"x")

    def test_only_dflash_strategies_reserve(self):
        for strategy in ("off", "ngram-mod", "draft-mtp", ""):
            self.assertEqual(
                bridge._resolve_spec_draft_for_tune(
                    "model.gguf", {"spec_strategy": strategy, "spec_draft_model": str(self.draft)}),
                "", strategy)

    def test_configured_draft_is_reserved(self):
        self.assertEqual(
            bridge._resolve_spec_draft_for_tune(
                "model.gguf", {"spec_strategy": "dflash", "spec_draft_model": str(self.draft)}),
            str(self.draft))

    def test_missing_path_falls_back_to_validated_pair(self):
        config = {"spec_strategy": "dflash",
                  "spec_draft_model": str(Path(self.tmp.name) / "gone.gguf")}
        with bridge_patch("find_spec_draft_for",
                          return_value={"matched": True, "path": str(self.draft)}):
            self.assertEqual(bridge._resolve_spec_draft_for_tune("model.gguf", config),
                             str(self.draft))

    def test_unvalidated_pair_is_not_reserved(self):
        with bridge_patch("find_spec_draft_for",
                          return_value={"matched": False, "path": str(self.draft)}):
            self.assertEqual(
                bridge._resolve_spec_draft_for_tune(
                    "model.gguf", {"spec_strategy": "dflash", "spec_draft_model": ""}),
                "")


class MatchPayloadTests(unittest.TestCase):
    """API responses must stay JSON-safe: the raw match carries a set."""

    def test_payload_is_slim_and_serializable(self):
        match = {
            "path": r"D:\m\dflash.gguf",
            "matched": True,
            "score": 170,
            "reasons": ["base model match: qwen3827b"],
            "draft": {"identities": {"qwen3827bdflash2", "qwen3827b"}},
        }
        payload = bridge.draft_match_payload(match)
        self.assertEqual(set(payload.keys()), {"path", "matched", "score", "reasons"})
        json.dumps(payload)  # raises if a set ever leaks back in

    def test_json_default_handles_sets_and_paths(self):
        body = json.dumps({"ids": {"b", "a"}, "p": Path(r"D:\x")},
                          default=bridge._json_default)
        self.assertIn('"a"', body)
        self.assertIn("D:", body)


class SpeculativeChoiceTests(unittest.TestCase):
    """The auto-tune pick: manual wins, then validated pair, then MTP."""

    def _choice(self, settings, has_mtp=False, is_moe=False, nextn=0, pair=None, kind="dflash"):
        return bridge._speculative_choice(settings, has_mtp, is_moe, nextn, pair or {}, kind)

    def test_manual_choice_always_wins(self):
        for strategy in ("off", "ngram-mod", "draft-mtp", "dflash", "dspark"):
            choice, note = self._choice(
                {"spec_strategy": strategy, "spec_strategy_source": "manual"},
                has_mtp=True, pair={"path": r"D:\m\dflash.gguf"})
            self.assertEqual(choice, strategy)
            self.assertIn("manual", note)

    def test_validated_pair_beats_mtp(self):
        choice, note = self._choice(
            {"spec_strategy": "draft-mtp", "spec_strategy_source": "auto"},
            has_mtp=True, pair={"path": r"D:\m\Qwen3.8-27B-DFlash2.gguf"})
        self.assertEqual(choice, "dflash")
        self.assertIn("Qwen3.8-27B-DFlash2.gguf", note)

    def test_pair_kind_is_preserved(self):
        choice, _ = self._choice(
            {"spec_strategy_source": "auto"}, pair={"path": "x"}, kind="dspark")
        self.assertEqual(choice, "dspark")

    def test_mtp_when_no_pair(self):
        choice, note = self._choice(
            {"spec_strategy": "draft-mtp", "spec_strategy_source": "auto"},
            has_mtp=True, nextn=1)
        self.assertEqual(choice, "draft-mtp")
        self.assertIn("MTP heads", note)

    def test_moe_without_pair_is_off(self):
        choice, _ = self._choice({"spec_strategy_source": "auto"}, is_moe=True)
        self.assertEqual(choice, "off")

    def test_dense_without_mtp_uses_ngram(self):
        choice, _ = self._choice({"spec_strategy_source": "auto"})
        self.assertEqual(choice, "ngram-mod")

    def test_invalid_manual_value_falls_through(self):
        choice, _ = self._choice(
            {"spec_strategy": "bogus", "spec_strategy_source": "manual"},
            has_mtp=True)
        self.assertEqual(choice, "draft-mtp")


class WatchdogDraftTests(unittest.TestCase):
    """A draft that crash-loops the server must not keep being retried."""

    def test_attempt_three_disables_and_persists_bad_draft(self):
        settings = {
            "spec_strategy": "dflash",
            "spec_draft_model": r"D:\m\dflash.gguf",
            "model_path": r"D:\m\target.gguf",
        }
        with bridge_patch("get_settings", return_value=dict(settings)), \
             bridge_patch("save_json") as save_json, \
             bridge_patch("_save_model_config") as model_cfg, \
             bridge_patch("broadcast_event"), \
             bridge_patch("inspect_model", return_value={}):
            bridge._watchdog_self_heal(3)
        saved = save_json.call_args.args[1]
        self.assertEqual(saved["spec_strategy"], "off")
        self.assertEqual(saved["spec_draft_model"], "")
        # The heal syncs the WHOLE degraded state (offload keys included) to
        # the model store in one call, so _apply_model_config can never
        # restore the crashing config on the next load.
        model_cfg.assert_called_once()
        synced = model_cfg.call_args.args[1]
        self.assertEqual(synced["spec_strategy"], "off")
        self.assertEqual(synced["spec_draft_model"], "")
        self.assertIn("num_gpu", synced)


if __name__ == "__main__":
    unittest.main()
