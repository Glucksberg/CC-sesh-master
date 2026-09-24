"""Unit tests for the pure session-index / event-reader logic.

Run with:  python -m unittest tests.test_dashboard -v

Covers behaviour changed by the observability fixes: Claude title
extraction, noise-type skipping, Codex/Kimi line-number cursors and the
OpenAI-message conversion used by the Hermes readers. The Windows process
collector needs live CIM access and is verified manually, not here.
"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location('serve_dashboard_under_test',
                                               REPO / 'serve-dashboard.py')
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

SI = mod.SessionIndex
SER = mod.SessionEventReader


def write_jsonl(path, events):
    path.write_text(''.join(
        json.dumps(e, ensure_ascii=False) + '\n' for e in events),
        encoding='utf-8')


class TitleExtractionTest(unittest.TestCase):
    def _extract(self, events, source='claude'):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'session.jsonl'
            write_jsonl(p, events)
            return SI._extract_metadata(p, p.stat().st_size, source)

    def test_custom_title_wins_over_ai_title(self):
        meta = self._extract([
            {'type': 'ai-title', 'aiTitle': 'Automatic title'},
            {'type': 'custom-title', 'customTitle': 'Chosen title'},
            {'type': 'ai-title', 'aiTitle': 'Later automatic'},
        ])
        self.assertEqual(meta['slug'], 'Chosen title')

    def test_ai_title_used_when_no_custom_title(self):
        meta = self._extract([
            {'type': 'ai-title', 'aiTitle': 'Automatic title'},
        ])
        self.assertEqual(meta['slug'], 'Automatic title')

    def test_agent_name_fills_when_no_titles(self):
        meta = self._extract([{'type': 'agent-name', 'agentName': 'Agent'}])
        self.assertEqual(meta['slug'], 'Agent')

    def test_utf8_accents_survive(self):
        meta = self._extract([
            {'type': 'custom-title', 'customTitle': 'Saudação amigável #2'},
        ])
        self.assertEqual(meta['slug'], 'Saudação amigável #2')

    def test_titles_ignored_for_non_claude_sources(self):
        meta = self._extract([
            {'type': 'custom-title', 'customTitle': 'Nope'},
        ], source='openclaw')
        self.assertNotEqual(meta.get('slug'), 'Nope')

    def test_empty_titles_are_ignored(self):
        meta = self._extract([
            {'type': 'custom-title', 'customTitle': '   '},
            {'type': 'ai-title', 'aiTitle': 'Valid'},
        ])
        self.assertEqual(meta['slug'], 'Valid')


class NoiseSkippingTest(unittest.TestCase):
    NOISE_TYPES = ['attachment', 'last-prompt', 'atis-latch', 'mode',
                   'permission-mode', 'ai-title', 'custom-title',
                   'agent-name', 'pr-link', 'queue-operation',
                   'file-history-snapshot']

    def test_tail_returns_only_real_events_with_uuid(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'session.jsonl'
            events = []
            for i in range(30):
                events.append({'type': 'user', 'uuid': f'u{i}',
                               'text': f'hello {i}'})
                # interleave uuid-less noise like Claude Code writes
                events.append({'type': self.NOISE_TYPES[i % len(self.NOISE_TYPES)]})
            write_jsonl(p, events)
            evs = SER._read_tail(p, p.stat().st_size, 50, None)
            self.assertEqual(len(evs), 30)
            self.assertTrue(all(e['uuid'] for e in evs))
            self.assertTrue(all(e['type'] == 'user' for e in evs))

    def test_read_full_applies_skip_list(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'session.jsonl'
            write_jsonl(p, [
                {'type': 'ai-title', 'aiTitle': 'x'},
                {'type': 'assistant', 'uuid': 'a1',
                 'message': {'model': 'm', 'content': [], 'usage': {}}},
            ])
            evs = SER._read_full(p, 100, None)
            self.assertEqual([e['uuid'] for e in evs], ['a1'])


class LineOffsetTest(unittest.TestCase):
    def setUp(self):
        SER._line_offset_cache.clear()
        self._orig_tail = mod.TAIL_READ_BYTES

    def tearDown(self):
        SER._line_offset_cache.clear()
        mod.TAIL_READ_BYTES = self._orig_tail

    def test_count_lines_before(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'f.jsonl'
            write_jsonl(p, [{'i': i} for i in range(100)])
            data = p.read_bytes()
            offset = data.index(b'\n') + 1  # after first line
            self.assertEqual(SER._count_lines_before(p, offset, len(data)), 1)

    def test_codex_tail_line_numbers_are_absolute(self):
        mod.TAIL_READ_BYTES = 1024
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'rollout-x.jsonl'
            lines = [json.dumps({'type': 'response_item',
                                 'payload': {'type': 'function_call',
                                             'name': 'Bash',
                                             'call_id': f'c{i}',
                                             'arguments': '{}'}})
                     for i in range(200)]
            p.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            size = p.stat().st_size
            # Tail window returns only events inside the last TAIL_READ_BYTES,
            # but each uuid must equal codex-<absolute line number>.
            evs = SER._read_codex_events(p, size, None, 500, None)
            self.assertTrue(evs)
            # determine returned window precisely via uuids
            uuids = {e['uuid'] for e in evs}
            self.assertTrue(all(u.startswith('codex-') for u in uuids))
            self.assertEqual(len(evs), len(uuids))  # no duplicates
            self.assertIn('codex-199', uuids)
            for e in evs:
                idx = int(e['uuid'].split('-')[1])
                self.assertEqual(e['tool_uses'][0]['name'], 'Bash')
            # after-cursor pagination picks up from an absolute line number
            after = SER._read_codex_events(p, size, 'codex-195', 500, None)
            self.assertEqual(after[0]['uuid'], 'codex-196')


class HermesConversionTest(unittest.TestCase):
    def test_select_converted_cursor_math(self):
        converted = [(k, {'type': 'user', 'uuid': f'hstate-{k}'})
                     for k in range(10)]
        evs = SER._select_converted(converted, 'hstate', 'hstate-3', 100, None)
        self.assertEqual([e['uuid'] for e in evs],
                         [f'hstate-{k}' for k in range(4, 10)])

    def test_select_converted_bad_cursor_returns_all(self):
        converted = [(k, {'type': 'user', 'uuid': f'hmsg-{k}'})
                     for k in range(5)]
        evs = SER._select_converted(converted, 'hmsg', 'garbage', 100, None)
        self.assertEqual(len(evs), 5)

    def test_system_role_skipped_and_tool_content_flattened(self):
        converted = SER._convert_openai_messages('hstate', [
            (0, {'role': 'system', 'content': 'secret'}),
            (1, {'role': 'user', 'content': [
                {'type': 'text', 'text': 'olá'},
                {'type': 'text', 'text': 'mundo'},
            ]}),
            (2, {'role': 'tool', 'tool_call_id': 't1',
                 'content': 'result text'}),
        ])
        self.assertEqual(len(converted), 2)
        kinds = [ev['type'] for _, ev in converted]
        self.assertEqual(kinds, ['user', 'user'])
        self.assertEqual(converted[0][1]['text'], 'olá\nmundo')
        self.assertEqual(converted[1][1]['tool_results'][0]['tool_use_id'],
                         't1')


class OpenCodeMessageTest(unittest.TestCase):
    def test_assembles_text_tools_and_results(self):
        parts = [
            {'type': 'text', 'text': 'olá mundo'},
            {'type': 'tool', 'tool': 'Bash', 'callID': 'call-1',
             'state': {'status': 'completed', 'input': {'cmd': 'ls'},
                       'output': 'ok'}},
            {'type': 'tool', 'tool': 'Bash', 'callID': 'call-2',
             'state': {'status': 'running', 'input': {}}},
        ]
        evs = SER._parse_opencode_message('ocmsg-m1', 'ts', 'assistant',
                                          {'role': 'assistant'}, parts)
        self.assertEqual(len(evs), 2)  # assistant + 1 completed result
        assistant = evs[0]
        self.assertEqual(assistant['text'], 'olá mundo')
        self.assertEqual(assistant['usage']['input_tokens'], 0)
        self.assertEqual([tu['name'] for tu in assistant['tool_uses']],
                         ['Bash', 'Bash'])
        self.assertEqual(evs[1]['tool_results'][0]['tool_use_id'], 'call-1')
        self.assertEqual(evs[1]['uuid'], 'ocmsg-m1-tr0')


class BenchEventTest(unittest.TestCase):
    def test_prediction_line_builds_qa_events(self):
        raw = {
            'messages': [{'role': 'user', 'content': 'Qual a capital?'}],
            'model_output': {'choices': [{'message': {'content': [
                {'type': 'text', 'text': 'Brasília'},
            ]}}],
                'usage': {'input_tokens': 10, 'output_tokens': 5},
                'time': 1.25},
            'metadata': {'correct_answer': 'Brasília'},
        }
        q, a = SER._build_bench_line_events(3, raw)
        self.assertEqual(q['uuid'], 'bench-3-q')
        self.assertEqual(q['text'], 'Qual a capital?')
        self.assertEqual(a['uuid'], 'bench-3-a')
        self.assertIn('Brasília', a['text'])
        self.assertIn('Correct answer', a['text'])
        self.assertEqual(a['usage']['input_tokens'], 10)
        self.assertEqual(a['latency_seconds'], 1.2)

    def test_retry_line_flags_verdict(self):
        q, a = SER._build_bench_line_events(7, {'sample_index': 3,
                                                'correct': True,
                                                'answer_extracted': True,
                                                'finish_reason': 'stop',
                                                'latency_s': 2})
        self.assertIn('recovery retry', q['text'])
        self.assertIn('correct', a['text'])


class HandlerHelpersTest(unittest.TestCase):
    def test_qint_falls_back(self):
        qs = {'offset': ['abc'], 'limit': ['50'], 'bad': [None]}
        self.assertEqual(mod.TrackerAPIHandler._qint(qs, 'offset', 0), 0)
        self.assertEqual(mod.TrackerAPIHandler._qint(qs, 'limit', 5), 50)
        self.assertEqual(mod.TrackerAPIHandler._qint(qs, 'missing', 7), 7)

    def test_compose_status_counts(self):
        h = object.__new__(mod.TrackerAPIHandler)
        procs = [
            {'type': 'claude-code', 'pid': '1', 'memory': 10, 'cpu': 0, 'state': 'S'},
            {'type': 'mcp-server', 'pid': '2', 'memory': 5, 'cpu': 0, 'state': 'S'},
            {'type': 'worker', 'pid': '3', 'memory': 1, 'cpu': 0, 'state': 'S'},
        ]
        system = {'available_mb': 1, 'total_mb': 2, 'swap_used_mb': 0}
        data = h._compose_status(procs, system, 16)
        self.assertIn('claude:1,mcp:1,worker:1,chroma:0', data['counts'])
        self.assertEqual(data['total_mem_mb'], 16)
        self.assertEqual(data['system_used_mb'], 1)


if __name__ == '__main__':
    unittest.main()
