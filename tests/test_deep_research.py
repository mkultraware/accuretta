import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch
from deep_research import ResearchStore, RESEARCH_TOOLS, brief


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = ResearchStore(Path(self.temporary.name) / 'research')
        self.chat = 'research-test'
        self.id = self.store.start(self.chat, {'topic': 'District heating', 'purpose': 'Compare options'})['id']

    def apply(self, name, **args):
        return self.store.apply(self.id, self.chat, name, args)['research']

    def source(self, url='https://example.org/report', content=None):
        content = content or 'An evaluated district heating network reduced energy demand by 12 percent in the study area. Costs varied across sites.'
        return self.store.observe(self.id, self.chat, 'web_fetch', {'url': url}, {'url': url, 'text': content})['source_id']

    def seed(self):
        self.apply('research_plan', questions=['What is it?', 'What is the evidence?', 'What are the costs?'])
        source = self.source()
        self.apply('research_note', question=1, source_id=source, quote='Costs varied across sites.',
                   finding='Costs were variable in this report.', relation='context')

    def publish(self, **kwargs):
        args = dict(title='Heating options', overview='A limited evidence review.',
                    slides=[{'title': title, 'summary': 'The report indicates variable costs.', 'note_ids': ['N1']}
                            for title in ['Context', 'Evidence', 'Implications']],
                    limitations=['Small evidence base.'], gaps=[{'question': 2, 'reason': 'More studies needed.'},
                                                              {'question': 3, 'reason': 'No comparable cost data.'}])
        args.update(kwargs)
        return self.apply('research_publish', **args)

    def test_brief_requires_topic(self):
        for value in ({}, None, {'topic': ' '}):
            with self.assertRaises(ValueError):
                brief(value)

    def test_search_snippets_do_not_become_citable_sources(self):
        self.store.observe(self.id, self.chat, 'web_search', {'query': 'district heating'},
                           {'results': [{'url': 'https://example.org', 'snippet': 'A claim'}]})
        data = self.store.get(self.id, self.chat)
        self.assertEqual(data['sources'], [])
        self.assertEqual(data['searches'], ['district heating'])

    def test_quotes_must_exist_in_read_sources(self):
        self.seed()
        for source, quote in [('S404', 'Costs varied across sites.'), ('S1', 'The study proved it always works.')]:
            with self.assertRaises(ValueError):
                self.apply('research_note', question=2, source_id=source, quote=quote, finding='Test', relation='supports')

    def test_changed_source_keeps_prior_evidence(self):
        self.seed()
        changed = self.source(content='This updated source contains different conclusions. ' * 3)
        self.assertEqual(changed, 'S2')
        self.assertEqual(self.store.get(self.id, self.chat)['notes'][0]['source_id'], 'S1')

    def test_coverage_and_saved_notes_required_to_publish(self):
        self.seed()
        with self.assertRaises(ValueError):
            self.publish(gaps=[])
        with self.assertRaises(ValueError):
            self.publish(slides=[{'title': 'X', 'summary': 'Y', 'note_ids': ['N404']}] * 3)

    def test_completed_notebook_persists_and_exposes_limits(self):
        self.seed()
        data = self.publish()
        self.assertEqual(data['status'], 'complete')
        self.assertTrue(any('independent corroboration' in item for item in data['presentation']['limitations']))
        self.assertNotIn('text', data['sources'][0])
        restored = ResearchStore(self.store.directory).get(self.id, self.chat)
        self.assertEqual(restored, data)
        self.assertEqual(self.store.finish(self.id, self.chat)['status'], 'complete')

    def test_incomplete_is_never_published(self):
        self.seed()
        data = self.store.finish(self.id, self.chat)
        self.assertEqual(data['status'], 'incomplete')
        self.assertIsNone(data['presentation'])

    def test_separate_chats_cannot_share_notebooks(self):
        with self.assertRaises(ValueError):
            self.store.get(self.id, 'someone-else')
        with self.assertRaises(ValueError):
            self.store.get('../outside', self.chat)

    def test_failed_fetch_does_not_count(self):
        self.assertIsNone(self.store.observe(self.id, self.chat, 'web_fetch', {'url': 'https://example.org'}, {'error': '403'}))
        self.assertEqual(len(self.store.get(self.id, self.chat)['sources']), 0)

    def test_real_dispatch_adds_source_id_and_blocks_execution(self):
        with bridge_patch('_research_store', self.store), \
             patch.dict(bridge._research_runs, {self.chat: self.id}), \
             bridge_patch('_get_current_chat', return_value=self.chat), \
             bridge_patch('_offline_active', False), \
             bridge_patch('_rt_context_chat', return_value={}), \
             bridge_patch('_selected_execution_target', return_value='host'), \
             bridge_patch('_emit_chat_event') as emit, \
             patch.dict(bridge.TOOLS, {'web_fetch': {**bridge.TOOLS['web_fetch'], 'fn': lambda args: {
                 'url': args['url'], 'text': 'A retrieved source with enough text to count as readable evidence. ' * 3}}}):
            bridge._reset_tool_call_history(self.chat)
            result = bridge._invoke_tool_with_context('web_fetch', {'url': 'https://example.net/report'}, 'fetch-1')
            self.assertEqual(result['research_source_id'], 'S1')
            self.assertEqual(emit.call_args[0][0]['type'], 'research_update')
            self.assertTrue(bridge.invoke_tool('run_powershell', {'command': 'echo nope'})['not_executed'])
            visible = bridge._visible_tool_names(chat_id=self.chat)
            self.assertTrue({'research_plan', 'research_note', 'research_publish', 'web_fetch'} <= visible)
            self.assertTrue(visible <= RESEARCH_TOOLS)

    def test_mode_normalization_preserves_research(self):
        self.assertEqual(bridge._normalize_composer_mode('research'), 'research')
        self.assertEqual(bridge._normalize_composer_mode('auto'), 'agent')

    def model_call(self, name, **args):
        with bridge_patch('_research_store', self.store), \
             bridge_patch('_research_context', return_value=(self.chat, self.id)), \
             bridge_patch('_emit_chat_event'):
            result = bridge._research_action(name, args)
        wire = bridge.compress_tool_result(name, result, bridge._tool_result_cap(name))
        self.assertLessEqual(len(wire), 4000)
        parsed = json.loads(wire)
        self.assertNotIn('_truncated', parsed)
        return parsed

    def test_large_notebook_ids_survive_real_model_compression_and_publish(self):
        self.seed()
        for i in range(2, 61):
            result = self.model_call('research_note', question=(i % 3) + 1, source_id='S1',
                quote='Costs varied across sites.', finding=f'Finding {i}: ' + 'Context varies by location. ' * 35,
                relation='context')
            self.assertEqual(result['note_id'], f'N{i}')
        data = self.store.get(self.id, self.chat)
        self.assertGreater(len(json.dumps(data)), 14636)
        index = self.model_call('research_notebook')
        self.assertEqual(index['note_ids'], [f'N{i}' for i in range(1, 61)])
        ids, offset = [], 0
        while offset is not None:
            page = self.model_call('research_notebook', view='notes', offset=offset)
            ids.extend(n['id'] for n in page['items'])
            offset = page['next_offset']
        self.assertEqual(ids, index['note_ids'])
        note = self.model_call('research_notebook', note_id='n13')['note']
        self.assertEqual(note['id'], 'N13')
        published = self.model_call('research_publish', title='Evidence review', overview='Recorded findings.',
            slides=[{'title': f'Question {i}', 'summary': 'Context varies by location.', 'note_ids': [f'n{i}']}
                    for i in range(1, 4)], limitations=['Single source.'], gaps=[])
        self.assertTrue(published['presentation_ready'])

    def test_saved_source_can_be_paged_without_refetching(self):
        body = ''.join(f'Section {i}: evidence about costs and geographic context.\n' for i in range(300))
        source = self.source(content=body)
        parts, offset = [], 0
        while offset is not None:
            page = self.model_call('research_notebook', source_id=source, offset=offset)
            parts.append(page['text'])
            offset = page['next_offset']
        self.assertEqual(''.join(parts), body.strip())

    def test_error_explains_recovery_and_keeps_unknown_ids_invalid(self):
        self.seed()
        error = self.model_call('research_note', question=2, source_id='S1', quote='This is an invented quote.',
                                finding='Unsupported finding.', relation='supports')
        self.assertIn('research_notebook(source_id="S1"', error['error'])
        error = self.model_call('research_publish', title='Review', overview='Review',
            slides=[{'title': 'Evidence', 'summary': 'Claim', 'note_ids': ['note-99']}] * 3,
            limitations=[], gaps=[{'question': 2, 'reason': 'No evidence'}, {'question': 3, 'reason': 'No evidence'}])
        self.assertIn('Valid IDs: N1', error['error'])

    def test_duplicate_save_returns_original_note_id(self):
        self.seed()
        result = self.model_call('research_note', question=1, source_id='S1', quote='Costs varied across sites.',
                                finding='Costs were variable in this report.', relation='context')
        self.assertEqual(result['note_id'], 'N1')
        self.assertTrue(result['already_saved'])

    def test_source_id_survives_last_resort_compression(self):
        wire = bridge.compress_tool_result('web_fetch', {'research_source_id': 'S12',
            'large_nested_payload': {'data': ['x' * 5000] * 10}}, 4000)
        self.assertEqual(json.loads(wire)['research_source_id'], 'S12')

    def test_resume_preserves_all_evidence_and_checks_ownership(self):
        self.seed()
        before = self.store.finish(self.id, self.chat)
        with self.assertRaises(ValueError):
            self.store.resume(self.id, 'other-chat')
        resumed = self.store.resume(self.id, self.chat)
        self.assertEqual(resumed['status'], 'running')
        for key in ['id', 'brief', 'sources', 'notes', 'questions']:
            self.assertEqual(before[key], resumed[key])
        self.publish()
        with self.assertRaises(ValueError):
            self.store.resume(self.id, self.chat)
