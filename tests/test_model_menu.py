"""Model menu: clean names, groups, families and bit levels from GGUF file
names and headers (model_display_info), the cached per-file read behind
/api/models, and the family logos the menu's rail draws: a rule per known
family, a name match against the shipped logos for other makers, else a
letter."""
import re
import unittest
from unittest.mock import patch

import bridge
from _patching import bridge_patch
from _source import frontend_source

APP = frontend_source()


def meta(arch="", org="", file_type=None):
    keys = {}
    if arch:
        keys["general.architecture"] = arch
    if org:
        keys["general.base_model.0.organization"] = org
    if file_type is not None:
        keys["general.file_type"] = file_type
    return {"architecture": arch, "keys": keys}


class DisplayNameTests(unittest.TestCase):
    def test_quant_and_build_tags_come_off_the_name(self):
        info = bridge.model_display_info("Qwen3.8-27B-UD-Q4_K_XL.gguf", meta("qwen35", "Qwen"))
        self.assertEqual(info["display_name"], "Qwen3.8 27B")
        self.assertEqual(info["quant"], "Q4_K_XL")
        self.assertEqual(info["quant_tags"], ["UD"])
        self.assertEqual(info["bits"], 4)

    def test_version_dots_and_casing_survive(self):
        cases = {
            "Nanbeige4.3-33B.i1-IQ4_XS.gguf": "Nanbeige4.3 33B",
            "Ornith-1.5-9B-Q8_0.gguf": "Ornith 1.5 9B",
            "OrcaSAQ-2-27B-Uncensored.gguf": "OrcaSAQ 2 27B Uncensored",
            "Sage-A1.5-Q4_K_M.gguf": "Sage A1.5",
            "Cyber-Tiel-Coder-35B-A3B-MTP-UD-Q4_K_XL.gguf": "Cyber Tiel Coder 35B A3B",
            "gemma-3-12b-it-Q8_0.gguf": "Gemma 3 12B IT",
            "gemma-3-27b-it-qat-Q4_0.gguf": "Gemma 3 27B IT",
            "gpt-oss-20b-MXFP4.gguf": "GPT OSS 20B",
            "Qwen3-235B-A22B-Q4_K_M-00001-of-00003.gguf": "Qwen3 235B A22B",
        }
        for name, want in cases.items():
            with self.subTest(name=name):
                self.assertEqual(bridge.model_display_info(name, {})["display_name"], want)

    def test_files_of_one_model_share_a_group(self):
        groups = {bridge.model_display_info(n, {})["group"] for n in (
            "Qwen3.8-27B-UD-Q4_K_M.gguf", "Qwen3.8-27B-UD-Q4_K_XL.gguf",
            "Qwen3.8-27B-AD-IQ4_XS.gguf", "Qwen3.8-27B-AD-Q4_K_M.gguf")}
        self.assertEqual(groups, {"qwen3.8 27b"})
        self.assertNotEqual(bridge.model_display_info("Qwen3.8-27B-Unleashed-UD-Q3_K_XL.gguf", {})["group"],
                            "qwen3.8 27b")

    def test_header_file_type_fills_a_name_without_a_quant(self):
        info = bridge.model_display_info("OrcaSAQ-2-27B-Uncensored.gguf", meta("qwen35", file_type=30))
        self.assertEqual((info["quant"], info["bits"]), ("IQ4_XS", 4))
        self.assertEqual(bridge.model_display_info("Mystery-7B.gguf", {})["bits"], 0)

    def test_bit_levels(self):
        for quant, bits in (("Q2_K", 2), ("Q3_K_XL", 3), ("IQ4_XS", 4), ("Q5_K_M", 5), ("Q6_K", 6),
                            ("Q8_0", 8), ("F16", 16), ("BF16", 16), ("F32", 32), ("MXFP4_MOE", 4)):
            with self.subTest(quant=quant):
                self.assertEqual(bridge.model_display_info(f"Model-7B-{quant}.gguf", {})["bits"], bits)


class FamilyTests(unittest.TestCase):
    def family(self, name, m):
        return bridge.model_display_info(name, m)["family"]

    def test_finetunes_keep_their_base_family(self):
        # base model's maker, then the architecture
        self.assertEqual(self.family("Clover-35B-A3B-Preview-Q4_K_M.gguf", meta("clover_moe", "Qwen")), "qwen")
        self.assertEqual(self.family("Cyber-Tiel-Coder-35B-A3B-MTP-UD-Q4_K_XL.gguf", meta("qwen35moe", "Ornith Ai")), "qwen")
        self.assertEqual(self.family("Ornith-1.5-9B-Q8_0.gguf", meta("qwen35")), "qwen")

    def test_maker_beats_a_borrowed_architecture(self):
        self.assertEqual(self.family("Kimi-K2-Instruct-Q2_K.gguf", meta("deepseek2", "moonshotai")), "kimi")

    def test_bare_llama_architecture_is_not_assumed_meta(self):
        self.assertEqual(self.family("DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf", meta("llama")), "deepseek")
        self.assertEqual(self.family("Llama-3.1-8B-Instruct-Q4_K_M.gguf", meta("llama")), "llama")
        info = bridge.model_display_info("Nanbeige4.3-33B.i1-IQ4_XS.gguf", meta("llama", "Nanbeige"))
        self.assertEqual((info["family"], info["family_label"]), ("nanbeige", "Nanbeige"))


class CachedReadTests(unittest.TestCase):
    def test_header_is_read_once_per_file_version(self):
        bridge._MODEL_INFO_CACHE.clear()
        entry = {"name": "Qwen3.8-27B-UD-Q4_K_M.gguf", "path": "X:/m/q.gguf", "size": 1, "modified_at": 1}
        with bridge_patch("read_gguf_metadata", return_value=meta("qwen35")) as read:
            bridge.model_display_info_for(entry)
            bridge.model_display_info_for(entry)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(read.call_args.kwargs, {"max_bytes": 1024 * 1024, "retry_bytes": 0})
            bridge.model_display_info_for({**entry, "modified_at": 2})
            self.assertEqual(read.call_count, 2)

    def test_only_a_name_without_a_quant_reads_the_full_header(self):
        bridge._MODEL_INFO_CACHE.clear()
        entry = {"name": "OrcaSAQ-2-27B-Uncensored.gguf", "path": "X:/m/o.gguf", "size": 1, "modified_at": 1}
        with bridge_patch("read_gguf_metadata",
                          side_effect=[meta("qwen35"), meta("qwen35", file_type=30)]) as read:
            info = bridge.model_display_info_for(entry)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(read.call_args_list[1].kwargs, {})
        self.assertEqual(info["quant"], "IQ4_XS")


class LogoTests(unittest.TestCase):
    def test_every_family_has_a_vendored_logo(self):
        for family, (label, logo) in bridge.MODEL_FAMILIES.items():
            if not logo:
                continue  # a wordmark or too fine at 18px: the letter reads better
            with self.subTest(family=family):
                self.assertTrue((bridge.ROOT / "assets/vendor/lobe-icons" / f"{logo}.svg").is_file())
        info = bridge.model_display_info("Qwen3.8-27B-UD-Q4_K_M.gguf", meta("qwen35"))
        self.assertEqual(info["family_logo"], "qwen")

    def test_inkling_gets_the_thinking_machines_logo(self):
        # Unsloth's layout (inkling2), llama.cpp's (inkling2_loop), the maker's name, and a bare file name
        for name, m in (("Inkling-2-Mini-UD-Q4_K_XL.gguf", meta("inkling2")),
                        ("Inkling-2-Mini-abliterated-Mix-16G.gguf", meta("inkling2_loop")),
                        ("Foo-7B-Q8_0.gguf", meta("newarch", "Thinking Machines Lab")),
                        ("Inkling-2-Mini-Q4_K_M.gguf", meta())):
            with self.subTest(name=name):
                info = bridge.model_display_info(name, m)
                self.assertEqual((info["family"], info["family_label"], info["family_logo"]),
                                 ("inkling", "Inkling", "thinkingmachines"))

    def test_an_unknown_maker_is_matched_to_a_shipped_logo(self):
        # no rule for the maker, but its name (less "AI", "Labs"...) fits a logo
        with patch.dict(bridge._FAMILY_BY_ORG, {}, clear=True):
            info = bridge.model_display_info("Foo-1.2B-Q8_0.gguf", meta("newarch", "LiquidAI"))
        self.assertEqual((info["family"], info["family_label"], info["family_logo"]), ("liquidai", "LiquidAI", "liquid"))
        self.assertEqual(bridge._match_logo("Nous Research"), "nousresearch")
        self.assertEqual(bridge._match_logo("Arcee AI"), "arcee")

    def test_no_logo_means_a_letter(self):
        info = bridge.model_display_info("Nanbeige4.3-33B.i1-IQ4_XS.gguf", meta("llama", "Nanbeige"))
        self.assertEqual(info["family_logo"], "")
        self.assertIn('class="wb-mp-letter"', APP)
        # the logo name is checked before it reaches a style attribute
        self.assertIn(r'/^[\w-]+$/.test(logo || "")', APP)

    def test_vendored_logos_are_plain_glyphs(self):
        # drawn as CSS masks in the text colour: no scripts, links or fixed fills
        for svg in (bridge.ROOT / "assets/vendor/lobe-icons").glob("*.svg"):
            with self.subTest(svg=svg.name):
                text = svg.read_text(encoding="utf-8")
                self.assertIn('fill="currentColor"', text)
                self.assertNotRegex(text, r"(?i)<script|\son\w+=|href=")


if __name__ == "__main__":
    unittest.main()
