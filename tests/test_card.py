import importlib.util
import json
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location('card_under_test', Path(__file__).parents[1] / 'card.py')
card = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = card
spec.loader.exec_module(card)


class CardTests(unittest.TestCase):
    def test_visual_hierarchy(self):
        state = card.State(question='question', text='answer')
        state.step('preparing')
        state.narratives.append('public plan')
        body = card.render(state, {}, state.text)
        elements = body['body']['elements']
        answer = next(e for e in elements if e.get('element_id') == 'answer_body')
        self.assertEqual(answer['text_size'], 'normal')
        self.assertEqual(answer['content'], 'answer')
        for e in elements[-2:]:
            self.assertEqual(e['text_size'], 'notation')
            self.assertEqual(e['text_color'], 'grey')
        process = next(e for e in elements if e['tag'] == 'collapsible_panel')
        self.assertEqual(process['background_color'], 'grey')
        self.assertIn('public plan', process['elements'][0]['content'])
        self.assertIn('icon', process['header'])
        self.assertEqual(sum(e['tag'] == 'hr' for e in elements), 2)

    def test_topic_heading_and_no_duplicate_quote(self):
        state = card.State(question='original question', text='## Topic summary\n\nFull answer', terminal='已完成')
        elements = card.render(state, {'show_question': True}, state.text)['body']['elements']
        title = next(e for e in elements if e.get('element_id') == 'answer_title')
        answer = next(e for e in elements if e.get('element_id') == 'answer_body')
        self.assertEqual(title['content'], '**Topic summary**')
        self.assertEqual(title['text_size'], 'heading')
        self.assertEqual(answer['content'], 'Full answer')
        self.assertNotIn('original question', json.dumps(elements))
        fallback = card.render(card.State(question='Topic'), {}, 'Plain answer')['body']['elements']
        self.assertEqual(next(e for e in fallback if e.get('element_id') == 'answer_body')['content'], 'Plain answer')

    def test_long_unicode_lossless(self):
        text = ('中文段落🐈\n' * 9000) + 'final'
        parts = card.pages(text)
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(len(p.encode()) <= 10000 for p in parts))

    def test_sources_fail_closed(self):
        for url in ['http://example.com', 'https://127.0.0.1/a', 'https://x.com/a?token=secret', 'https://user:pass@x.com', 'https://x.local/a', 'https://x.com/a)\nmalicious']:
            self.assertEqual(card.source_url(url), '')
        self.assertEqual(card.source_url('https://example.com/doc'), 'https://example.com/doc')
        state = card.State()
        state.source('source', 'https://example.com/doc')
        state.source('source', 'https://example.com/doc')
        self.assertEqual(len(state.sources), 1)

    def test_usage_terminal_only_and_multiple_models(self):
        state = card.State()
        usage = SimpleNamespace(input_other=10, input_cached=4, output=3)
        for _ in range(5):
            state.response('a', SimpleNamespace(is_chunk=True, usage=usage))
        state.response('a', SimpleNamespace(is_chunk=False, usage=usage))
        state.response('b', SimpleNamespace(is_chunk=False, usage=usage))
        state.response('b', SimpleNamespace(is_chunk=False, usage=None))
        text = json.dumps(card.render(state, {}, 'answer'), ensure_ascii=False)
        self.assertIn('↑28 ↓6', text)
        self.assertIn('已返回用量', text)
        self.assertNotIn('估算', text)

    def test_large_metadata_stays_under_platform_limit(self):
        state = card.State()
        for i in range(20):
            state.source('文' * 120 + str(i), 'https://example.com/' + str(i) + 'a' * 900, '知' * 30)
        for i in range(12):
            state.step(str(i) + '进' * 170)
        state.tools = [{'name': '工' * 80, 'start': state.start, 'status': '执行中'} for _ in range(16)]
        answer = '中' * 3300
        body = card.render(state, {}, answer)
        self.assertLessEqual(len(json.dumps(body, ensure_ascii=False).encode()), 26000)
        self.assertIn(answer, json.dumps(body, ensure_ascii=False))

    def test_card_bounded_and_no_raw_reasoning(self):
        state = card.State(question='question')
        for i in range(100):
            state.step('step' * 100)
            state.source('source' + str(i), 'https://example.com/' + str(i))
        body = card.render(state, {}, '正文' * 1000)
        self.assertEqual(body['schema'], '2.0')
        self.assertLess(len(json.dumps(body, ensure_ascii=False).encode()), 28000)
        self.assertEqual(len(state.sources), 20)
        self.assertNotIn('思考过程', json.dumps(body, ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
