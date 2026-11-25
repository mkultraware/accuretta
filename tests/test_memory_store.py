import json
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import bridge
from _patching import bridge_patch
from memory_store import MemoryStore, PREFERENCE_BUDGET


class MemoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = MemoryStore(Path(directory.name) / "memories.jsonl")

    def test_preferences_do_not_evict_each_other_or_include_facts(self):
        for number in range(20):
            self.store.put({"text": f"Preference {number}", "kind": "preference"})
        self.store.put({"text": "A private project fact", "kind": "fact"})
        with bridge_patch("_memory_store", self.store):
            selected = bridge._select_memories_for_prompt()
        self.assertEqual(len(selected), 20)
        self.assertFalse(any("project fact" in item["text"] for item in selected))

    def test_old_memories_are_searchable_facts_until_explicitly_reclassified(self):
        self.store.save([{"id": "old", "text": "Use plain English"}])
        self.assertEqual(self.store.profile()["legacy_count"], 1)
        self.assertEqual(self.store.profile()["preferences"], [])
        self.assertEqual(self.store.search("English")["total"], 1)
        self.store.put({"id": "old", "kind": "preference"}, edit=True)
        self.assertEqual(len(self.store.profile()["preferences"]), 1)

    def test_budget_refuses_addition_without_removing_or_shortening(self):
        for letter in "abc":
            self.store.put({"text": letter * 2000, "kind": "preference"})
        before = self.store.path.read_bytes()
        result = self.store.put({"text": "One more", "kind": "preference"})
        self.assertTrue(result["profile_full"])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.profile()["characters"], PREFERENCE_BUDGET)
        self.assertTrue(self.store.put({"text": "Fact still fits", "kind": "fact"})["saved"])

    def test_edit_replaces_only_selected_preference_and_keeps_kind(self):
        first = self.store.put({"text": "Explain briefly", "kind": "preference"})
        second = self.store.put({"text": "Use plain English", "kind": "preference"})
        self.store.put({"id": first["id"], "text": "Explain in detail"}, edit=True)
        entries = {item["id"]: item for item in self.store.read()}
        self.assertEqual(entries[first["id"]]["kind"], "preference")
        self.assertEqual(entries[second["id"]]["text"], "Use plain English")
        self.assertEqual(len(entries), 2)

    def test_search_words_tags_pagination_and_restart(self):
        for number in range(12):
            self.store.put({"text": f"Decision {number}", "tags": ["colour"]})
        store = MemoryStore(self.store.path)
        result = store.search("COLOUR", limit=5)
        self.assertEqual(result["total"], 12)
        self.assertEqual(result["next_offset"], 5)
        self.assertEqual(len(store.search("colour", offset=10)["memories"]), 2)
        self.assertEqual(store.search("unrelated")["total"], 0)

    def test_overlong_text_is_not_silently_cut(self):
        self.assertIn("error", self.store.put({"text": "a" * 2001}))
        self.assertEqual(self.store.read(), [])

    def test_parallel_saves_do_not_lose_notes(self):
        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(lambda n: self.store.put({"text": f"Note {n}"}), range(30)))
        self.assertEqual(len(self.store.read()), 30)

    def test_damaged_store_is_not_overwritten(self):
        self.store.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.put({"text": "New note"})
        self.assertEqual(self.store.path.read_text(), "{broken")

    def test_full_prompt_includes_all_preferences_and_no_fact(self):
        for n in range(5):
            self.store.put({"text": f"Standing instruction {n}", "kind": "preference"})
        self.store.put({"text": "SEARCHABLE_ONLY_SECRET", "kind": "fact"})
        with bridge_patch("_memory_store", self.store):
            prompt = bridge.build_system_prompt(False)
        for n in range(5):
            self.assertIn(f"Standing instruction {n}", prompt)
        self.assertNotIn("SEARCHABLE_ONLY_SECRET", prompt)
        self.assertIn("search_memories", prompt)


if __name__ == "__main__":
    unittest.main()
