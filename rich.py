"""Native card documents and component recipes; no business routing."""
import copy
import json

DOCS = 'https://open.feishu.cn/document/feishu-cards/'
RECIPES = {
    'code': {'schema': '2.0', 'body': {'elements': [
        {'tag': 'markdown', 'content': '```python\nprint("Hello")\n```'}]}},
    'table': {'schema': '2.0', 'body': {'elements': [{'tag': 'table', 'page_size': 10,
        'columns': [{'name': 'item', 'display_name': '项目', 'data_type': 'text'},
                    {'name': 'amount', 'display_name': '数值', 'data_type': 'number'}],
        'rows': [{'item': '示例', 'amount': 10}]}]}},
    'chart': {'schema': '2.0', 'body': {'elements': [{'tag': 'chart', 'chart_spec': {
        'type': 'bar', 'data': [{'id': 'data', 'values': [{'item': 'A', 'value': 10}, {'item': 'B', 'value': 20}]}],
        'xField': 'item', 'yField': 'value'}}]}},
    'image': {'schema': '2.0', 'body': {'elements': [{'tag': 'img', 'img_key': 'USE_REAL_IMAGE_KEY',
        'alt': {'tag': 'plain_text', 'content': '图片说明'}}]}},
    'layout': {'schema': '2.0', 'body': {'elements': [{'tag': 'column_set', 'columns': [
        {'tag': 'column', 'width': 'weighted', 'weight': 1, 'elements': [{'tag': 'markdown', 'content': '**左栏**'}]},
        {'tag': 'column', 'width': 'weighted', 'weight': 1, 'elements': [{'tag': 'markdown', 'content': '**右栏**'}]}]}]}},
    'button': {'schema': '2.0', 'body': {'elements': [{'tag': 'button',
        'text': {'tag': 'plain_text', 'content': '继续分析'}, 'type': 'primary',
        'behaviors': [{'type': 'callback', 'value': {'action': '继续分析当前结果'}}]}]}},
    'form': {'schema': '2.0', 'body': {'elements': [{'tag': 'form', 'name': 'details', 'elements': [
        {'tag': 'input', 'name': 'requirements', 'placeholder': {'tag': 'plain_text', 'content': '补充需求'}},
        {'tag': 'button', 'name': 'submit', 'form_action_type': 'submit',
         'text': {'tag': 'plain_text', 'content': '提交'}, 'type': 'primary',
         'behaviors': [{'type': 'callback', 'value': {'action': '按提交的需求继续处理'}}]}]}]}},
}


def guide(component='all'):
    result = {
        'format': 'Native Feishu Card JSON 2.0; all component fields pass through unchanged except callback binding and managed streaming options.',
        'components': ['markdown (code fences, lists, links, mentions, formulas where supported)',
                       'img / img_combination / person / person_list / chart / table / hr',
                       'column_set / column / collapsible_panel / interactive_container / form',
                       'button / input / select_static / multi_select_static / select_person / multi_select_person',
                       'select_img / overflow / date_picker / picker_time / picker_datetime / checker'],
        'docs': DOCS + 'card-json-v2-structure',
        'rules': ['Use code fences with language for code; never execute code merely to display it.',
                  'Choose table for exact comparisons, chart for trends, columns for side-by-side summaries.',
                  'Use feishu_card_upload_image for local generated image assets, then pass the returned img_key; never invent resource keys or business data.',
                  'Callback behaviors use value.action to describe the requested continuation; plugin binds the operator and current conversation.',
                  'After successful rendering, do not repeat the card in the final text. Supply a complete fallback_text.',
                  'For components absent from recipes, pass their documented native JSON; Feishu validates it. Correct errors returned by the tool.',
                  'Card limits, client versions, app permissions and callback subscription still apply.',
                  'Audio/video/files unsupported by a card component must use the host native attachment tools; do not invent card tags.'],
        'recipes': RECIPES if component == 'all' else {component: RECIPES.get(component)},
    }
    return json.dumps(result, ensure_ascii=False)


def parse_card(card_json):
    if not isinstance(card_json, str) or len(card_json.encode()) > 24000:
        raise ValueError('card_json must be a JSON string within 24 KB; split large content into separate concise views.')
    card = json.loads(card_json)
    if not isinstance(card, dict) or card.get('schema') != '2.0':
        raise ValueError('Use native schema 2.0 with body.elements. Legacy JSON 1.0/templates are not CardKit documents; convert or resolve the template first.')
    if not isinstance(card.get('body'), dict) or not isinstance(card['body'].get('elements'), list):
        raise ValueError('body.elements must be an array.')
    # Do not whitelist tags: new official components remain usable without plugin releases.
    result = copy.deepcopy(card)
    result.setdefault('config', {}).pop('streaming_mode', None)
    result['config']['update_multi'] = True
    result['config']['wide_screen_mode'] = True
    return result
