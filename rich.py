"""Native card documents and component recipes; no business routing."""
import copy
import json
import csv
import io
import zipfile
import unicodedata

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
                  'Keep identifiers and names complete: never abbreviate or replace suffixes with ellipses. Tables use automatic row height; the plugin sets pixel column widths for native horizontal scrolling. Never duplicate tables as row-by-row detail panels.',
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
        raise ValueError('Table exceeds the card budget. Split the output into smaller views while preserving ALL original values; do not abbreviate or omit rows.')
    return result


def display_value(value):
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    return json.dumps(value, ensure_ascii=False)


def expand_tables(node):
    """Size native columns for overflow; never duplicate or truncate source rows."""
    if isinstance(node, list):
        for child in node:
            expand_tables(child)
    elif isinstance(node, dict):
        if node.get('tag') == 'table':
            columns, rows = node.get('columns', []), node.get('rows', [])
            if columns and all(isinstance(c, dict) for c in columns) and all(isinstance(r, dict) for r in rows):
                node['row_height'] = 'auto'
                for column in columns:
                    values = [display_value(column.get('display_name', column.get('name', '')))]
                    values += [display_value(row.get(column.get('name'), '')) for row in rows]
                    units = max((sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1
                                     for c in line) for value in values for line in value.splitlines()), default=0)
                    # Pixel widths preserve overflow; percentage/auto widths squeeze long identifiers.
                    needed = min(600, max(120, units * 9 + 32))
                    existing = column.get('width', '')
                    if isinstance(existing, str) and existing.endswith('px'):
                        try:
                            needed = max(needed, int(existing[:-2]))
                        except ValueError:
                            pass
                    column['width'] = f'{needed}px'
        for key, value in node.items():
            if key not in ('rows', 'chart_spec'):
                expand_tables(value)


def reply_archive(state):
    """Export only delivered answer data in memory; omit runtime callback capabilities."""
    output = io.BytesIO()
    native = copy.deepcopy(state.rich_card)
    tables = []
    def clean(node):
        if isinstance(node, list):
            for child in node:
                clean(child)
        elif isinstance(node, dict):
            if node.get('tag') == 'table':
                tables.append(node)
            if isinstance(node.get('behaviors'), list):
                node['behaviors'] = [b for b in node['behaviors'] if b.get('type') != 'callback']
            for key, value in node.items():
                if key not in ('rows', 'chart_spec'):
                    clean(value)
    clean(native)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('reply.md', state.text)
        archive.writestr('README.txt', '回复状态：' + state.terminal +
            '\nreply.md 为完整正文或原生卡片文字兜底。card.json 保留原生组件数据（已移除交互回调）。'
            '\n表格另存为 CSV，保留所有已提交行；图表数据在 card.json 中。图片为飞书资源引用，未打包图片二进制。'
            '\n导出不含隐藏推理、工具原始参数、聊天历史或内部配置。\n')
        if native:
            archive.writestr('card.json', json.dumps(native, ensure_ascii=False, indent=2))
        for i, table in enumerate(tables, 1):
            csv_text = io.StringIO(newline='')
            writer = csv.writer(csv_text)
            columns = table.get('columns', [])
            writer.writerow([c.get('display_name', c.get('name', '')) for c in columns])
            for row in table.get('rows', []):
                writer.writerow([display_value(row.get(c.get('name'), '')) for c in columns])
            archive.writestr(f'tables/table-{i}.csv', csv_text.getvalue().encode('utf-8-sig'))
    return output.getvalue()
