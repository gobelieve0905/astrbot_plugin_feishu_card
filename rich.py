"""Native card documents and component recipes; no business routing."""
import copy
import json
import html
import re

DOCS = 'https://open.feishu.cn/document/feishu-cards/'
RECIPES = {
    'code': {'schema': '2.0', 'body': {'elements': [
        {'tag': 'markdown', 'content': '```python\nprint("Hello")\n```'}]}},
    'table': {'schema': '2.0', 'body': {'elements': [{'tag': 'table', 'page_size': 10, 'row_height': 'auto',
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
                  'Keep identifiers and names complete: never abbreviate or replace suffixes with ellipses. Tables use automatic row height; long cells also receive a full-width detail panel. Use row-oriented text for very wide records.',
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
    expand_tables(result)
    if len(json.dumps(result, ensure_ascii=False).encode()) > 24000:
        raise ValueError('Full table details exceed the card budget. Use a row-oriented Markdown reply with ALL original values so the plugin can paginate it; do not abbreviate or omit rows.')
    return result


def display_value(value):
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    return json.dumps(value, ensure_ascii=False)


def escaped_cell(value):
    # Cell data remains literal; do not turn a name into a link or card markup.
    return re.sub(r"([\\`*_\[\]#|~])", r"\\\1", html.escape(display_value(value), quote=False))


def expand_tables(node):
    """Add full-width data views to long/wide tables, leaving every source cell intact."""
    if isinstance(node, list):
        for child in node:
            expand_tables(child)
    elif isinstance(node, dict):
        for key, children in list(node.items()):
            if key != 'elements' or not isinstance(children, list):
                if key not in ('rows', 'chart_spec'):
                    expand_tables(children)
                continue
            expanded = []
            for element in children:
                expand_tables(element)
                expanded.append(element)
                if not isinstance(element, dict) or element.get('tag') != 'table':
                    continue
                columns, rows = element.get('columns', []), element.get('rows', [])
                if not columns or not rows or not all(isinstance(c, dict) for c in columns) or not all(isinstance(r, dict) for r in rows):
                    continue
                element['row_height'] = 'auto'
                long_names = {c.get('name') for c in columns if any(len(display_value(r.get(c.get('name'), ''))) > 28 for r in rows)}
                if not long_names and len(columns) <= 4:
                    continue
                if len(columns) == 2 and len(long_names) == 1 and not any('width' in c for c in columns):
                    for column in columns:
                        column['width'] = '70%' if column.get('name') in long_names else '30%'
                details = []
                for index, row in enumerate(rows, 1):
                    lines = [f"**第 {index} 行**"]
                    for column in columns:
                        name = column.get('name')
                        if name in row:
                            title = column.get('display_name') or name
                            lines.append(f"**{escaped_cell(title)}**：{escaped_cell(row[name])}")
                    details.append({'tag': 'markdown', 'content': '\n\n'.join(lines), 'text_size': 'normal'})
                expanded.append({'tag': 'collapsible_panel', 'expanded': False,
                    'header': {'title': {'tag': 'plain_text', 'content': f'完整表格内容（{len(rows)} 行）'},
                               'icon': {'tag': 'standard_icon', 'token': 'down-small-ccm_outlined', 'size': '16px 16px'},
                               'icon_position': 'follow_text', 'icon_expanded_angle': -180},
                    'elements': details})
            node[key] = expanded
