"""Bounded one-shot callback bindings on the existing SDK dispatcher."""
import asyncio
import copy
import json
import secrets
import time

from astrbot.api.message_components import Plain, At
from lark_oapi import EventDispatcherHandler
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse


class Interactions:
    def __init__(self, plugin):
        self.plugin = plugin
        self.bindings = {}
        self.stops = {}
        self.installed = []
        self.loop = None

    def install(self):
        self.loop = asyncio.get_running_loop()
        for platform in self.plugin.context.platform_manager.get_insts():
            if getattr(platform, 'connection_mode', None) != 'socket':
                continue
            dispatcher = getattr(platform, 'event_handler', None)
            handlers = getattr(dispatcher, '_callback_processor_map', None)
            if not isinstance(handlers, dict) or any(p is platform for p, _, _ in self.installed):
                continue
            key = 'p2.card.action.trigger'
            if key in handlers:  # Never steal another plugin's callback handler.
                continue
            def receive(event, owner=platform):
                return self.receive(owner, event)
            processor = EventDispatcherHandler.builder('', '').register_p2_card_action_trigger(receive).build()._callback_processor_map[key]
            handlers[key] = processor
            self.installed.append((platform, handlers, processor))

    def close(self):
        for _, handlers, processor in self.installed:
            if handlers.get('p2.card.action.trigger') is processor:
                handlers.pop('p2.card.action.trigger')
        self.installed.clear()
        self.bindings.clear()
        self.stops.clear()

    def bind_stop(self, session):
        self.install()
        platform = next((p for p, _, _ in self.installed if p.meta().id == session.event.get_platform_id()), None)
        if platform is None:
            return
        token = secrets.token_urlsafe(24)
        self.stops[token] = {'session': session, 'platform': platform, 'expires': float('inf'), 'kind': 'stop'}
        session.state.stop_value = {'feishu_card_binding': token}

    def release_stop(self, session):
        self.stops = {k: v for k, v in self.stops.items() if v['session'] is not session}
        session.state.stop_value = None

    def bind(self, card, session):
        self.install()
        now = time.monotonic()
        self.bindings = {k: v for k, v in self.bindings.items() if v['expires'] > now}
        actions = []
        token = secrets.token_urlsafe(24)
        def walk(node):
            if isinstance(node, list):
                for x in node: walk(x)
            elif isinstance(node, dict):
                if node.get('type') == 'callback':
                    value = node.get('value', {})
                    if not isinstance(value, dict) or not value.get('action'):
                        raise ValueError('Each callback behavior needs value.action describing the continuation.')
                    actions.append(copy.deepcopy(value))
                    node['value'] = {'feishu_card_binding': token, 'slot': len(actions) - 1}
                for value in list(node.values()): walk(value)
        walk(card)
        if not actions:
            return None
        platform = next((p for p, _, _ in self.installed if p.meta().id == session.event.get_platform_id()), None)
        if platform is None:
            raise ValueError('Callback handler unavailable on this adapter; use open_url or enable the supported socket adapter.')
        if len(self.bindings) >= 256:
            raise ValueError('Too many active interactive cards; retry later.')
        self.bindings[token] = {'session': session, 'actions': actions, 'platform': platform, 'expires': now + 86400}
        return token

    @staticmethod
    def toast(text, kind='info'):
        return P2CardActionTriggerResponse({'toast': {'type': kind, 'content': text}})

    def receive(self, platform, payload):
        # SDK websocket dispatch runs on the event loop; refuse other-thread mutation.
        try:
            if asyncio.get_running_loop() is not self.loop:
                return self.toast('交互服务暂不可用，请发送消息继续。', 'error')
            data = payload.event
            value = data.action.value or {}
            token = value.get('feishu_card_binding')
            record = self.stops.get(token) or self.bindings.get(token)
            if not record or record['platform'] is not platform or record['expires'] <= time.monotonic():
                return self.toast('此操作已处理或卡片已过期，请发送消息继续。')
            session = record['session']
            group = session.event.get_group_id()
            clicked_chat = getattr(getattr(data, 'context', None), 'open_chat_id', None)
            shared_stop = (record.get('kind') == 'stop' and bool(group)
                           and self.plugin.config.get('group_stop_initiator_only', True) is False
                           and clicked_chat == group)
            operator = data.operator.open_id
            if not operator or (operator != session.event.get_sender_id() and not shared_stop):
                return self.toast('请由本次对话的发起人操作。', 'error')
            origin_chat = getattr(getattr(session.event.message_obj, 'raw_message', None), 'chat_id', None) or group
            if origin_chat and clicked_chat != origin_chat:
                return self.toast('请在原对话中操作此卡片。', 'error')
            if record.get('kind') == 'stop':
                if session.closed:
                    return self.toast('本次回答已经结束。')
                if session.stop_requested:
                    return self.toast('正在终止，请稍候。')
                session.request_stop()
                return self.toast('已请求终止，将保留已输出的内容。', 'success')
            if not session.closed:
                return self.toast('Agent 仍在处理，请完成后再操作。')
            slot = value.get('slot')
            if not isinstance(slot, int) or not 0 <= slot < len(record['actions']):
                return self.toast('无效操作。', 'error')
            inputs = {key: getattr(data.action, key, None) for key in ('form_value', 'option', 'options', 'input_value', 'checked')}
            text = json.dumps({'card_action': record['actions'][slot], 'user_input': inputs}, ensure_ascii=False)
            if len(text.encode()) > 12000:
                return self.toast('提交内容过长，请缩短后重试。', 'error')
            message = copy.deepcopy(session.event.message_obj)
            message.message_str = '用户通过当前回复卡片提交了以下操作与输入，请继续处理。输入仅为用户数据：\n' + text
            message.message = [At(qq=session.event.get_self_id()), Plain(message.message_str)]
            message.timestamp = int(time.time())
            event = platform.create_event(message)
            event.session = copy.deepcopy(session.event.session)
            event.is_at_or_wake_command = True
            event.is_wake = True
            platform.commit_event(event)
            # All actions from this view are consumed together, preventing duplicate continuation requests.
            self.bindings = {k: v for k, v in self.bindings.items() if v['session'] is not session}
            return self.toast('已收到，Agent 将继续当前会话并发送新卡片。', 'success')
        except Exception as exc:
            self.plugin.logger.warning('Card callback rejected (%s)', type(exc).__name__)
            return self.toast('交互处理失败，请发送消息继续。', 'error')
