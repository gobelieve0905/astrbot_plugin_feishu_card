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


rich_spec = importlib.util.spec_from_file_location('rich_under_test', Path(__file__).parents[1] / 'rich.py')
rich = importlib.util.module_from_spec(rich_spec)
rich_spec.loader.exec_module(rich)


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
        self.assertEqual(sum(e['tag'] == 'hr' for e in elements), 1)

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

    def test_native_document_keeps_layout_and_auxiliary_order(self):
        native = {'schema': '2.0', 'header': {'title': {'tag': 'plain_text', 'content': 'Native title'}},
                  'body': {'elements': [{'tag': 'column_set', 'columns': []},
                                        {'tag': 'markdown', 'content': '```python\nprint(1)\n```'}]}}
        state = card.State(rich_card=native)
        state.step('processing')
        result = card.render(state, {}, 'fallback')
        self.assertEqual(result['header'], native['header'])
        self.assertEqual(result['body']['elements'][:2], native['body']['elements'])
        self.assertEqual(len(native['body']['elements']), 2)
        self.assertEqual(result['body']['elements'][2]['tag'], 'hr')
        self.assertEqual(result['body']['elements'][3]['tag'], 'collapsible_panel')

    def test_code_pages_keep_fences_and_lossless_source(self):
        source = '```python\n' + ('print("test")\n' * 2000) + '```'
        chunks = card.pages(source)
        self.assertEqual(''.join(chunks), source)
        for i, chunk in enumerate(chunks):
            display = card.markdown_page(source, chunk, i)
            self.assertTrue(display.startswith('```python\n'))
            self.assertTrue(display.rstrip().endswith('```'))
        titled = '## Code example\n\n' + source
        state = card.State(text=titled, terminal='已完成')
        first = card.render(state, {}, card.pages(titled)[0])
        content = next(e['content'] for e in first['body']['elements'] if e.get('element_id') == 'answer_body')
        self.assertTrue(content.startswith('```python\n'))
        self.assertTrue(content.rstrip().endswith('```'))

    def test_stop_button_running_stopping_and_terminal(self):
        state = card.State(stop_value={'feishu_card_binding': 'test'})
        for native in (None, {'schema': '2.0', 'body': {'elements': []}}):
            state.rich_card = native
            state.terminal = ''
            state.stopping = False
            body = card.render(state, {}, 'partial')['body']['elements']
            button = next(e for e in body if e.get('element_id') == 'stop_answer')
            self.assertFalse(button['disabled'])
            state.stopping = True
            body = card.render(state, {}, 'partial')['body']['elements']
            self.assertTrue(next(e for e in body if e.get('element_id') == 'stop_answer')['disabled'])
            state.terminal = '已终止'
            self.assertNotIn('stop_answer', str(card.render(state, {}, 'partial')))

    def test_long_table_keeps_all_values_in_full_width_details(self):
        name = 'Example_Identifier_With_Multiple_Segments_And_Unique_Suffix_123456789'
        source = {'schema': '2.0', 'body': {'elements': [{'tag': 'table', 'row_height': 'low',
            'columns': [{'name': 'name', 'display_name': 'Name', 'data_type': 'text'},
                        {'name': 'value', 'display_name': 'Value', 'data_type': 'number'}],
            'rows': [{'name': name, 'value': 123.456}, {'name': 'short', 'value': 0}]}]}}
        prepared = rich.parse_card(json.dumps(source))
        table, details = prepared['body']['elements']
        self.assertEqual(table['rows'], source['body']['elements'][0]['rows'])
        self.assertEqual(table['row_height'], 'auto')
        self.assertEqual([c['width'] for c in table['columns']], ['70%', '30%'])
        text = str(details)
        self.assertIn(rich.escaped_cell(name), details['elements'][0]['content'])
        self.assertIn('123.456', text)
        self.assertIn('short', text)
        self.assertIn('0', details['elements'][1]['content'])
        state = card.State(rich_card=prepared)
        state.narratives = ['metadata ' * 4000]
        rendered = card.render(state, {}, 'fallback')
        self.assertEqual(rendered['body']['elements'][1], details)

    def test_wide_nested_tables_and_short_tables(self):
        table = {'tag': 'table', 'columns': [{'name': str(i), 'width': '100px'} for i in range(5)],
                 'rows': [{str(i): i for i in range(5)}]}
        source = {'schema': '2.0', 'body': {'elements': [{'tag': 'column_set', 'columns': [
            {'tag': 'column', 'elements': [table]}]}]}}
        result = rich.parse_card(json.dumps(source))
        elements = result['body']['elements'][0]['columns'][0]['elements']
        self.assertEqual(len(elements), 2)
        self.assertEqual(elements[0]['rows'], table['rows'])
        self.assertTrue(all(c['width'] == '100px' for c in elements[0]['columns']))
        result = rich.parse_card(json.dumps(rich.RECIPES['table']))
        self.assertEqual(len(result['body']['elements']), 1)

    def test_expanded_table_budget_fails_without_dropping_values(self):
        source = {'schema': '2.0', 'body': {'elements': [{'tag': 'table',
            'columns': [{'name': 'name'}], 'rows': [{'name': 'x' * 8000}, {'name': 'y' * 8000}]}]}}
        with self.assertRaisesRegex(ValueError, 'ALL original values'):
            rich.parse_card(json.dumps(source))
        self.assertEqual(len(source['body']['elements'][0]['rows']), 2)

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
