"""Native card documents and component recipes; no business routing."""
import copy
import json
import re
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
        'rules': ['Serialize the complete card object exactly once for card_json; escape quotes and backslashes in string values. Do not manually concatenate JSON or shorten data to fix encoding errors.',
                  'Use code fences with language for code; never execute code merely to display it.',
                  'Choose table for exact comparisons, chart for trends, columns for side-by-side summaries.',
                  'Keep identifiers and names complete: never abbreviate or replace suffixes with ellipses. Tables use automatic row height; the plugin sets pixel column widths for native horizontal scrolling. Never duplicate tables as row-by-row detail panels.',
                  'Use feishu_card_upload_image for local generated image assets, then pass the returned img_key; never invent resource keys or business data.',
                  'Callback behaviors use value.action to describe the requested continuation; plugin validates original-chat permissions and submits continuation metadata through the native event queue; it does not restore topics itself.',
                  'Continuation controls share one successful submission per card across all users; bindings expire after 24 hours or reload. The plugin adds a visible notice automatically.',
                  'After successful rendering, do not repeat the card in the final text. Supply a complete fallback_text.',
                  'For components absent from recipes, pass their documented native JSON; Feishu validates it. Correct errors returned by the tool.',
                  'Card limits, client versions, app permissions and callback subscription still apply.',
                  'Audio/video/files unsupported by a card component must use the host native attachment tools; do not invent card tags.'],
        'recipes': RECIPES if component == 'all' else {component: RECIPES.get(component)},
    }
    return json.dumps(result, ensure_ascii=False)


def decode_card(card_json):
    """Decode bounded transport wrappers without rewriting card data or guessing syntax."""
    if not isinstance(card_json, str) or len(card_json.encode()) > 24000:
        raise ValueError('card_json must be a JSON string within 24 KB; split large content into separate concise views.')
    text = card_json.strip()
    for _ in range(3):
        fence = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, flags=re.DOTALL | re.IGNORECASE)
        if fence:
            text = fence.group(1)
        try:
            # strict=False only accepts literal control characters inside strings.
            # json.dumps at transport time escapes them again without altering content.
            decoded = json.loads(text, strict=False)
        except json.JSONDecodeError as exc:
            raise ValueError(f'卡片 JSON 格式错误：第 {exc.lineno} 行、第 {exc.colno} 列（{exc.msg}）。'
                             '请用 JSON 序列化生成 card_json；字符串中的双引号和反斜杠必须转义。'
                             '不要删减表格或改动原数据；也可直接输出完整 fallback_text。') from None
        if not isinstance(decoded, str):
            return decoded
        text = decoded.strip()
        if len(text.encode()) > 24000:
            break
    raise ValueError('card_json 重复编码层数过多；请仅序列化一次完整 JSON 2.0 对象。')


def parse_card(card_json):
    card = decode_card(card_json)
    if not isinstance(card, dict) or card.get('schema') != '2.0':
        raise ValueError('Use native schema 2.0 with body.elements. Legacy JSON 1.0/templates are not CardKit documents; convert or resolve the template first.')
    if not isinstance(card.get('body'), dict) or not isinstance(card['body'].get('elements'), list):
        raise ValueError('body.elements must be an array.')
    # Do not whitelist tags: new official components remain usable without plugin releases.
    result = copy.deepcopy(card)
    if 'config' in result and not isinstance(result['config'], dict):
        raise ValueError('config must be an object.')
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


def reply_markdown(state):
    """One portable Markdown document; preserve full native table/chart data."""
    sections = [state.text]
    if state.terminal and state.terminal != '已完成':
        sections.insert(0, '> 回复状态：' + state.terminal + '\n')
    def cell(value):
        return display_value(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('|', '&#124;').replace('\r\n', '\n').replace('\n', '<br>')
    def walk(node):
        if isinstance(node, list):
            for x in node: walk(x)
        elif isinstance(node, dict):
            if node.get('tag') == 'table':
                columns = node.get('columns', [])
                if columns:
                    lines = ['| ' + ' | '.join(cell(c.get('display_name', c.get('name', ''))) for c in columns) + ' |',
                             '| ' + ' | '.join('---' for c in columns) + ' |']
                    lines += ['| ' + ' | '.join(cell(row.get(c.get('name'), '')) for c in columns) + ' |' for row in node.get('rows', [])]
                    sections.append('### 完整表格\n\n' + '\n'.join(lines))
            elif node.get('tag') == 'chart':
                data = json.dumps(node.get('chart_spec', {}), ensure_ascii=False, indent=2)
                fence = '`' * max(3, max((len(x) for x in re.findall(r'`+', data)), default=0) + 1)
                sections.append('### 图表数据\n\n' + fence + 'json\n' + data + '\n' + fence)
            for key, value in node.items():
                if key not in ('rows', 'chart_spec', 'behaviors'):
                    walk(value)
    walk(state.rich_card)
    return '\n\n'.join(sections).encode('utf-8')
