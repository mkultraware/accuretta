import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch
from agent_reliability import EvidenceStore, compare_responses, feedback, response_state, review_evidence, verification_advice


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = EvidenceStore(self.root / 'recon_evidence' / 'receipts')
        self.target = 'https://example.test/private'

    def observe(self, body='Protected test account data', target=None, status=200, **extra):
        receipt = self.store.record('chat', 'mission', '', 'observe_response', {'url': target or self.target},
                                    {'status': status, 'body': body, 'headers': {}, **extra})
        return self.store.read('chat', 'mission', receipt['id'])

    def claim(self, records, control):
        return {'title': 'Access boundary', 'target': self.target, 'description': 'Controlled test account sees another test account resource.',
                'severity': 'high', 'status': 'validated', 'evidence_ids': [r['id'] for r in records],
                'challenge': {'observations': [{'evidence_id': r['id'], 'quote': r['result'].get('body') or r['result'].get('text', '')} for r in records],
                              'control': {'evidence_id': control['id'], 'quote': control['result'].get('body') or control['result'].get('text', '')},
                              'test': {'method': 'GET', 'location': 'none', 'parameter': '',
                                       'assertion': {'path': ['body'], 'operation': 'contains', 'value': 'Protected test account data'}},
                              'expected_boundary': 'Each test account owns only its own resource.',
                              'demonstrated_impact': 'One controlled resource was returned across the expected boundary.',
                              'alternative_explanation': 'The resource could have been intentionally public.',
                              'disproof_check': 'The anonymous control returned public content only.',
                              'limitations': 'Only two controlled accounts and one endpoint were examined.'}}

    def test_exact_receipts_survive_restart_and_isolate_missions(self):
        record = self.observe()
        self.assertEqual(EvidenceStore(self.store.root).read('chat', 'mission', record['id']), record)
        with self.assertRaises(FileNotFoundError):
            self.store.read('chat', 'another-mission', record['id'])
        with self.assertRaises(ValueError):
            self.store.read('chat', 'mission', '../outside')

    def test_shortened_or_quoted_ids_resolve_to_one_record(self):
        record = self.observe()
        for quoted in (record['id'][:10], f'"{record["id"].upper()}"', ' ' + record['id'][:8] + ' '):
            self.assertEqual(self.store.read('chat', 'mission', quoted)['id'], record['id'])
        with self.assertRaises(ValueError):
            self.store.read('chat', 'mission', record['id'][:5])   # too short to trust

    def test_changed_evidence_is_rejected(self):
        record = self.observe()
        record['result']['body'] = 'Changed after capture'
        (self.store.folder('chat', 'mission') / (record['id'] + '.json')).write_text(json.dumps(record))
        with self.assertRaises(ValueError):
            self.store.read('chat', 'mission', record['id'])

    def test_comparison_never_promotes_http_differences(self):
        records = [self.observe('Public baseline contents'), self.observe(), self.observe('Public control contents')]
        comparison = compare_responses(records)
        self.assertFalse(comparison['test_matches_baseline'])
        self.assertNotIn('validated', comparison)
        self.assertEqual(len(comparison['responses']), 3)
        with self.assertRaises(ValueError):
            compare_responses([records[0]] * 3)

    def test_authentication_failures_and_truncation_are_inconclusive(self):
        self.assertEqual(response_state({'status': 302, 'headers': {'Location': '/login'}}), 'authentication_unavailable')
        self.assertEqual(response_state({'status': 403}), 'authentication_unavailable')
        self.assertEqual(response_state({'status': 200, 'potentially_truncated': True}), 'inconclusive')
        self.assertEqual(response_state({'error': 'timeout'}), 'inconclusive')

    def test_review_requires_quotes_exact_target_and_separate_reproduction(self):
        records = [self.observe(), self.observe()]
        control = self.observe('Public control contents')
        claim = self.claim(records, control)
        self.assertTrue(review_evidence(claim, records, claim['challenge'], control)['eligible'])
        wrong = copy.deepcopy(records)
        wrong[1]['target'] = 'https://other.test/private'
        self.assertFalse(review_evidence(claim, wrong, claim['challenge'])['eligible'])
        claim['challenge']['observations'][0]['quote'] = 'Invented evidence never captured'
        self.assertFalse(review_evidence(claim, records, claim['challenge'])['eligible'])

    def test_coverage_pages_preserve_all_observations(self):
        for i in range(55):
            self.observe(str(i))
        first = self.store.coverage('chat', 'mission')
        self.assertEqual(first['total'], 55)
        self.assertEqual(first['next_offset'], 50)
        self.assertEqual(len(self.store.coverage('chat', 'mission', offset=50)['observations']), 5)

    def test_reproduction_must_pass_same_assertion_and_control_must_fail(self):
        for body, control_body in [('Ordinary search form page', 'Public control contents'),
                                   ('Protected test account data', 'Protected test account data')]:
            records = [self.observe(), self.observe(body)]
            control = self.observe(control_body)
            claim = self.claim(records, control)
            with self.context():
                self.assertFalse(bridge.tool_review_finding(claim)['eligible'])

    def test_post_claim_rejects_get_and_missing_parameter(self):
        records = [self.observe(), self.observe()]
        control = self.observe('Public control contents')
        claim = self.claim(records, control)
        claim['challenge']['test'].update(method='POST', location='body', parameter='query')
        with self.context():
            review = bridge.tool_review_finding(claim)
        self.assertFalse(review['eligible'])
        self.assertTrue(any('method' in issue for issue in review['issues']))
        self.assertTrue(any('parameter' in issue for issue in review['issues']))

    def test_post_reproduction_records_actual_body_parameters(self):
        def capture(body):
            summary = self.store.record('chat', 'mission', '', 'http_request',
                                        {'url': self.target, 'body': 'query=controlled-test'},
                                        {'method': 'POST', 'status': 200, 'body': body})
            return self.store.read('chat', 'mission', summary['id'])
        records = [capture('Protected test account data'), capture('Protected test account data')]
        control = capture('Public control contents')
        claim = self.claim(records, control)
        claim['challenge']['test'].update(method='POST', location='body', parameter='query')
        with self.context():
            self.assertTrue(bridge.tool_review_finding(claim)['eligible'])

    def test_scanner_summary_and_legacy_metadata_cannot_promote(self):
        records = [self.observe(), self.observe()]
        control = self.observe('Public control contents')
        claim = self.claim(records, control)
        records[0]['tool'] = 'sandbox_sqlmap'
        self.assertFalse(review_evidence(claim, records, claim['challenge'], control)['eligible'])
        records[0]['tool'] = 'observe_response'
        records[0].pop('request_context')
        self.assertFalse(review_evidence(claim, records, claim['challenge'], control)['eligible'])

    def test_control_rejects_different_endpoint_and_matching_missing_field(self):
        records = [self.observe(), self.observe()]
        control = self.observe('Public control contents', target='https://example.test/other')
        claim = self.claim(records, control)
        with self.context():
            self.assertFalse(bridge.tool_review_finding(claim)['eligible'])
        claim['challenge']['test']['assertion']['path'] = ['headers', 'absent']
        self.assertFalse(review_evidence(claim, records, claim['challenge'], control)['eligible'])

    def test_justified_equivalent_control_is_allowed(self):
        records = [self.observe(), self.observe()]
        control = self.observe('Public control contents', target='https://example.test/public')
        claim = self.claim(records, control)
        claim['challenge']['test']['control_strategy'] = 'equivalent'
        claim['challenge']['control']['purpose'] = 'The public route is the documented unauthenticated comparison.'
        with self.context():
            self.assertTrue(bridge.tool_review_finding(claim)['eligible'])

    def test_numeric_and_browser_assertions_are_supported(self):
        def capture(tool, args, result):
            summary = self.store.record('chat', 'mission', '', tool, args, result)
            return self.store.read('chat', 'mission', summary['id'])
        records = [capture('http_request', {'url': self.target}, {'method': 'GET', 'status': 201, 'body': 'created'}),
                   capture('http_request', {'url': self.target}, {'method': 'GET', 'status': 201, 'body': 'created'})]
        control = capture('http_request', {'url': self.target}, {'method': 'GET', 'status': 200, 'body': 'normal'})
        claim = self.claim(records, control)
        claim['challenge']['test']['assertion'] = {'path': ['status'], 'operation': 'equals', 'value': 201}
        with self.context():
            self.assertTrue(bridge.tool_review_finding(claim)['eligible'])

        browser_records = [capture('rt_browser', {'action': 'navigate', 'url': self.target}, {'text': 'Controlled marker shown', 'url': self.target, 'ok': True}),
                           capture('rt_browser', {'action': 'navigate', 'url': self.target}, {'text': 'Controlled marker shown', 'url': self.target, 'ok': True})]
        browser_control = capture('rt_browser', {'action': 'navigate', 'url': self.target}, {'text': 'Ordinary page', 'url': self.target, 'ok': True})
        browser_claim = self.claim(browser_records, browser_control)
        browser_claim['challenge']['test'] = {'method': 'BROWSER', 'location': 'interaction', 'parameter': 'navigate',
                                               'assertion': {'path': ['text'], 'operation': 'contains', 'value': 'Controlled marker'}}
        with self.context():
            self.assertTrue(bridge.tool_review_finding(browser_claim)['eligible'])

    def test_get_payload_variations_match_one_finding_endpoint(self):
        records = [self.observe(target=self.target + '?id=1'), self.observe(target=self.target + '?id=2')]
        control = self.observe('Public control contents', target=self.target + '?id=0')
        claim = self.claim(records, control)
        claim['challenge']['test'].update(location='query', parameter='id')
        with self.context():
            self.assertTrue(bridge.tool_review_finding(claim)['eligible'])

    def test_pentest_mode_controls_active_tools_not_finding_status(self):
        settings = {'red_team_enabled': True, 'rt_mission_state': True, 'rt_force_exploit': False}
        recon = {'mission': {'engagement': 'recon', 'phase': 'recon'}}
        pentest = {'mission': {'engagement': 'pentest', 'phase': 'recon'}}
        self.assertFalse(bridge._rt_incl_exploit_for(recon, settings))
        self.assertTrue(bridge._rt_incl_exploit_for(pentest, settings))
        self.assertTrue(bridge._rt_allow_matches('recon_dns', 'example.test:81', 'example.test', None))
        self.assertFalse(bridge._rt_allow_matches('recon_port_scan', 'example.test:81', 'example.test', None))
        self.assertFalse(bridge._rt_allow_matches('recon_dns', 'example.test:81', 'other.test', None))

    def test_partial_scans_and_server_errors_are_inconclusive(self):
        for result in [{'fetch_errors': [{'error': 'failed bundle'}]}, {'status': 504},
                       {'scan_warnings': ['server recovery warning']}]:
            self.assertEqual(response_state(result), 'inconclusive')

    def test_metadata_tampering_is_detected(self):
        record = self.observe()
        record['request_context']['method'] = 'POST'
        (self.store.folder('chat', 'mission') / (record['id'] + '.json')).write_text(json.dumps(record))
        with self.assertRaises(ValueError):
            self.store.read('chat', 'mission', record['id'])

    def test_report_rechecks_legacy_claim_and_preserves_gaps(self):
        records = [self.observe(), self.observe('Ordinary search form page')]
        control = self.observe('Public control contents')
        claim = self.claim(records, control)
        claim.update(id='finding', mission_id='mission', review={'challenge': claim.pop('challenge')})
        chat = {'id': 'chat', 'mission': {'authorized': True, 'authorization_id': 'mission', 'target': self.target, 'status': 'active'},
                'findings': [claim], 'security_coverage': [{'mission_id': 'mission', 'feature': 'Unlinked check', 'state': 'observed'}]}
        token = bridge._current_chat_id.set('chat')
        try:
            with self.context(), bridge_patch('DATA', self.root), bridge_patch('get_settings', return_value={'red_team_enabled': True}), bridge_patch('get_chats', return_value={'chats': {'chat': chat}}), bridge_patch('save_json'):
                result = bridge.tool_rt_generate_report({'findings': 'Unsupported full database write access.'})
            report = Path(result['report']).read_text(encoding='utf-8')
            self.assertEqual(result['validated_findings'], 0)
            self.assertFalse(result['assessment_complete'])
            self.assertEqual(result['unresolved_planned_checks'], 1)
            self.assertIn('Verification gaps', report)
            self.assertIn('Unverified analyst narrative', report)
            self.assertIn('Recorded checks', report)
            self.assertEqual(chat['mission']['status'], 'closed')
            self.assertEqual(chat['findings'][0]['status'], 'candidate')
        finally:
            bridge._current_chat_id.reset(token)

    def test_sqlmap_limits_load_and_exposes_service_warnings(self):
        output = "sqlmap identified the following injection point\nParameter: query (POST)\n[WARNING] HTTP error codes detected during run: 504\nback-end DBMS: SQLite"
        with bridge_patch('get_settings', return_value={'red_team_enabled': True}), bridge_patch('_rt_scope_block', return_value=None), bridge_patch('_emit_execution_event'), bridge_patch('request_approval', return_value={'decision': 'approve'}), bridge_patch('sandbox_run', return_value={'output': output, 'exit_code': 0, 'executed': True}) as run:
            result = bridge.tool_sandbox_sqlmap({'url': self.target})
        command = run.call_args.args[0]
        self.assertIn('--technique=BEU', command)
        self.assertIn('--threads=1', command)
        self.assertEqual(result['verification_status'], 'candidate')
        self.assertEqual(response_state(result), 'inconclusive')
        with bridge_patch('get_settings', return_value={'red_team_enabled': True}), bridge_patch('_rt_scope_block', return_value=None), bridge_patch('_emit_execution_event'), bridge_patch('request_approval', return_value={'decision': 'approve'}), bridge_patch('sandbox_run', return_value={'output': output, 'exit_code': 0, 'executed': True}) as timed_run:
            timed = bridge.tool_sandbox_sqlmap({'url': self.target, 'techniques': 'BEUT', 'time_sec': 2})
        self.assertIn('--technique=BEUT', timed_run.call_args.args[0])
        self.assertIn('--time-sec=2', timed_run.call_args.args[0])
        self.assertEqual(timed['techniques'], 'BEUT')

    def test_malformed_contract_fails_without_crashing(self):
        records = [self.observe(), self.observe()]
        control = self.observe('Public control contents')
        claim = self.claim(records, control)
        claim['challenge']['test']['location'] = {'unexpected': True}
        with self.context():
            self.assertFalse(bridge.tool_review_finding(claim)['eligible'])

    def context(self):
        return bridge_patch('_evidence_context', return_value=(self.store, 'chat', 'mission'))

    def test_bridge_control_must_exist_and_be_separate(self):
        records = [self.observe(), self.observe()]
        claim = self.claim(records, self.observe('Public control contents'))
        with self.context():
            self.assertTrue(bridge.tool_review_finding(claim)['eligible'])
            claim['challenge']['control'] = claim['challenge']['observations'][0]
            self.assertFalse(bridge.tool_review_finding(claim)['eligible'])
            claim['challenge']['control']['evidence_id'] = 'f' * 32
            self.assertFalse(bridge.tool_review_finding(claim)['eligible'])

    def test_unrelated_success_cannot_validate_finding(self):
        chat = {'id': 'chat', 'activity': [{'tool': 'observe_response', 'status': 'ok'}]}
        token = bridge._current_chat_id.set('chat')
        try:
            with bridge_patch('get_chats', return_value={'chats': {'chat': chat}}), bridge_patch('save_json'), self.context(), bridge_patch('_rt_context_chat', return_value=chat):
                result = bridge.tool_record_finding({'title': 'Unsupported claim', 'status': 'validated', 'source_tool': 'observe_response'})
                self.assertEqual(result['finding']['status'], 'candidate')
                self.assertFalse(result['finding']['source_observed'])
        finally:
            bridge._current_chat_id.reset(token)

    def test_heuristics_cannot_unlock_exploit_phase(self):
        self.assertFalse(bridge._rt_result_confirms_finding('validate_finding', {'likely_real': True}))
        self.assertFalse(bridge._rt_result_confirms_finding('recon_injection_probe', {'findings': [{'severity': 'critical'}]}))

    def test_observation_blocks_unsafe_headers_without_fetching(self):
        with bridge_patch('_recon_fetch') as fetch:
            self.assertTrue(bridge.tool_observe_response({'url': self.target, 'headers': {'Host': 'other.test'}})['not_executed'])
            fetch.assert_not_called()

    def test_observation_uses_no_redirect_capture(self):
        with bridge_patch('_recon_fetch', return_value={'status': 302, 'body': '', 'headers': {'Location': '/login'}}) as fetch:
            result = bridge.tool_observe_response({'url': self.target})
            self.assertFalse(fetch.call_args.kwargs['follow'])
            self.assertEqual(result['status'], 302)

    def test_coverage_does_not_turn_expired_session_into_clean_result(self):
        record = self.observe(status=401)
        chat = {}
        with self.context(), bridge_patch('get_chats', return_value={'chats': {'chat': chat}}), bridge_patch('save_json'):
            result = bridge.tool_security_coverage({'feature': 'Account view', 'target': self.target, 'identity': 'Test account A', 'evidence_ids': [record['id']]})
            self.assertEqual(result['planned_checks'][0]['state'], 'authentication_unavailable')

    def test_http_request_model_view_is_short_and_points_to_receipt(self):
        body = ('<html><head><title>Account</title></head><body>'
                + 'Secret account data. ' * 1200 + '</body></html>')
        receipt = {'id': 'a' * 32, 'tool': 'http_request', 'target': self.target}
        full = {'ok': True, 'status': 200, 'method': 'GET', 'url': self.target,
                'final_url': self.target, 'content_type': 'text/html',
                'headers': {'Content-Type': 'text/html', 'Set-Cookie': 'sid=test'},
                'set_cookie': ['sid=test'], 'body': body, 'elapsed_ms': 12.5,
                'attempt': 1, 'potentially_truncated': False,
                'request_identity_fingerprint': 'fingerprint', 'user_agent': 'test-agent',
                'evidence_receipt': receipt}
        rendered = bridge.compress_tool_result('http_request', full, 16000)
        parsed = json.loads(rendered)
        self.assertLess(len(rendered), 5000)
        self.assertNotIn(body, rendered)
        self.assertEqual(parsed['status'], 200)
        self.assertEqual(parsed['body']['characters'], len(body))
        self.assertEqual(parsed['body']['preview']['title'], 'Account')
        self.assertIn('Secret account data', parsed['body']['preview']['text'])
        self.assertEqual(parsed['full_response']['evidence_id'], receipt['id'])
        self.assertIn('security_coverage', parsed['full_response']['read_with'])

    def test_http_request_without_receipt_keeps_body(self):
        full = {'ok': True, 'status': 200, 'method': 'GET', 'url': self.target,
                'content_type': 'text/plain', 'headers': {}, 'set_cookie': [],
                'body': 'small response body'}
        parsed = json.loads(bridge.compress_tool_result('http_request', full, 16000))
        self.assertEqual(parsed['body'], 'small response body')

    def test_evidence_reader_pages_plain_response_text(self):
        body = 'line one\n' * 3000 + 'FINAL_MARKER'
        record = self.store.record('chat', 'mission', '', 'http_request', {'url': self.target},
                                   {'status': 200, 'headers': {'Content-Type': 'text/plain'}, 'body': body})
        with self.context():
            first = bridge.tool_security_coverage({'evidence_id': record['id'], 'offset': 0})
            second = bridge.tool_security_coverage({'evidence_id': record['id'], 'offset': first['next_offset']})
            third = bridge.tool_security_coverage({'evidence_id': record['id'], 'offset': second['next_offset']})
        self.assertEqual(first['read_mode'], 'response')
        self.assertNotIn('result_excerpt', first)
        self.assertEqual(first['content']['text'], body[:12000])
        self.assertEqual(first['next_offset'], 12000)
        self.assertEqual(first['headers']['Content-Type'], 'text/plain')
        self.assertEqual(second['content']['text'], body[12000:24000])
        self.assertNotIn('headers', second)
        self.assertEqual(third['content']['text'], body[24000:])
        self.assertIn('FINAL_MARKER', third['content']['text'])
        self.assertIsNone(third['next_offset'])

    def test_previous_response_comparison_reports_identity_and_changes(self):
        bridge._HTTP_RESPONSE_HISTORY.clear()
        self.addCleanup(bridge._HTTP_RESPONSE_HISTORY.clear)
        self.assertIsNone(bridge._http_compare_with_previous(
            'GET', self.target, 'anonymous', 200, {'Content-Type': 'text/html'}, 'public', []))
        comparison = bridge._http_compare_with_previous(
            'GET', self.target, 'explicit credentials', 200,
            {'Content-Type': 'text/html', 'X-Role': 'admin'}, 'private', ['sid=test'])
        self.assertEqual(comparison['previous_identity'], 'anonymous')
        self.assertTrue(comparison['body_changed'])
        self.assertEqual(comparison['headers_added'], ['x-role'])
        self.assertTrue(comparison['set_cookie_changed'])


class GuidanceTests(unittest.TestCase):
    def test_failure_categories_preserve_original_error(self):
        for name, result, category in [
            ('edit_file', {'error': 'file stale', 'stale': True}, 'stale_content'),
            ('read_file', {'error': 'missing path', 'missing_arguments': ['path']}, 'invalid_arguments'),
            ('run_tests', {'exit_code': 1, 'stdout': 'One assertion failed'}, 'check_failed'),
            ('write_file', {'error': 'user denied write'}, 'permission_denied'),
            ('read_file', {'error': 'limit reached', 'loop_breaker': True}, 'repeat_blocked'),
            ('some_tool', {'error': 'module not installed'}, 'missing_dependency'),
        ]:
            value = feedback(name, result)
            self.assertEqual(value['failure_guidance']['category'], category)
            for key in result:
                self.assertEqual(value[key], result[key])

    def test_guidance_separates_behavior_from_syntax_and_sign_in(self):
        self.assertIn('before editing', verification_advice(kind='bug'))
        self.assertIn('sign-in', verification_advice(kind='startup'))
        self.assertIn('interaction', verification_advice('app.css'))
        self.assertNotIn('verified', bridge.tool_verification_guidance({'kind': 'ui'}))

    def test_builtin_recipes_discoverable_without_personal_skills(self):
        with tempfile.TemporaryDirectory() as directory, bridge_patch('SKILLS_DIR', Path(directory) / 'missing'):
            catalog = bridge._scan_skills()
            self.assertTrue({'rt-access-control', 'rt-exposure-review', 'rt-signal-validation'} <= catalog.keys())


if __name__ == '__main__':
    unittest.main()
