"""Bounded one-shot callback bindings on the existing SDK dispatcher."""
import asyncio
import copy
import json
import hashlib
import secrets
import time
from types import MethodType, SimpleNamespace

from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType

from astrbot.api.message_components import Plain, At
from lark_oapi import EventDispatcherHandler
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse


CONTINUATION_KEY = 'conversation_continuation_v1'


def bind_delivery_anchor(event, origin_message_id):
    """Keep pipeline IDs synthetic while native Feishu delivery uses a real message ID.

    A separate delivery object avoids temporarily mutating the shared incoming event.
    Instance wrappers survive Session.restore() and do not patch AstrBot classes.
    """
    delivery = copy.copy(event)
    delivery.message_obj = copy.deepcopy(event.message_obj)
    delivery.message_obj.message_id = origin_message_id
    synthetic_id = event.message_obj.message_id
    native_card = event._send_card_message

    async def send(_event, chain):
        try:
            return await delivery.send(chain)
        finally:
            _event._has_send_oper = getattr(_event, '_has_send_oper', False) or getattr(delivery, '_has_send_oper', False)

    async def stream(_event, generator, use_fallback=False):
        try:
            return await delivery.send_streaming(generator, use_fallback=use_fallback)
        finally:
            _event._has_send_oper = getattr(_event, '_has_send_oper', False) or getattr(delivery, '_has_send_oper', False)

    async def card(_event, card_id, reply_message_id=None, receive_id=None, receive_id_type=None):
        if reply_message_id == synthetic_id or (reply_message_id is None and receive_id is None):
            reply_message_id = origin_message_id
        return await native_card(card_id, reply_message_id=reply_message_id,
                                 receive_id=receive_id, receive_id_type=receive_id_type)

    event.send = MethodType(send, event)
    event.send_streaming = MethodType(stream, event)
    event._send_card_message = MethodType(card, event)
    event._feishu_card_origin_message_id = origin_message_id


class Interactions:
    def __init__(self, plugin):
        self.plugin = plugin
        self.bindings = {}
        self.stops = {}
        self.downloads = {}
        self.download_tasks = set()
        self.installed = []
        self.loop = None
        self.closed = False

    def install(self):
        if self.closed:
            return
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
        self.closed = True
        for _, handlers, processor in self.installed:
            if handlers.get('p2.card.action.trigger') is processor:
                handlers.pop('p2.card.action.trigger', None)
        self.installed.clear()
        self.bindings.clear()
        self.stops.clear()
        self.downloads.clear()
        for task in self.download_tasks:
            task.cancel()

    def bind_download(self, session, content):
        self.install()
        platform = next((p for p, _, _ in self.installed if p.meta().id == session.event.get_platform_id()), None)
        if platform is None:
            raise ValueError('download callback unavailable')
        now = time.monotonic()
        self.downloads = {k: v for k, v in self.downloads.items() if v['expires'] > now}
        if len(content) > 2_000_000:
            raise ValueError('download too large')
        while self.downloads and (len(self.downloads) >= 256 or sum(len(v['content']) for v in self.downloads.values()) + len(content) > 16_000_000):
            self.downloads.pop(next(iter(self.downloads)))
        token = secrets.token_urlsafe(24)
        self.downloads[token] = {'platform': platform, 'transport': session.transport,
                                 'content': content, 'expires': now + 86400, 'busy': set(), 'recent': {}}
        session.state.download_value = {'feishu_download': token}

    def download(self, platform, data, token):
        record = self.downloads.get(token)
        operator = data.operator.open_id
        if not self.plugin.config.get('enable_reply_download', True):
            return self.toast('下载功能已关闭。')
        if not record or record['platform'] is not platform or record['expires'] <= time.monotonic() or not operator:
            return self.toast('下载已过期，请重新生成回复。', 'error')
        # Any authenticated viewer may download, including viewers of forwarded cards.
        now = time.monotonic()
        record['recent'] = {k: t for k, t in record['recent'].items() if now - t < 10}
        if operator in record['busy'] or operator in record['recent']:
            return self.toast('已收到下载请求，请稍候查看机器人私聊。')
        if len(self.download_tasks) >= 16:
            return self.toast('下载繁忙，请稍后重试。')
        record['busy'].add(operator)
        record['recent'][operator] = now
        async def deliver():
            try:
                await record['transport'].send_download(record['content'], operator)
            except Exception as exc:
                self.plugin.logger.warning('Markdown download failed (%s)', type(exc).__name__)
                try:
                    await record['transport'].send_private(operator, 'text', {'text': '回复下载失败，请稍后重试或联系管理员检查文件上传与消息权限。'})
                except Exception:
                    self.plugin.logger.warning('Download failure notification unavailable')
            finally:
                record['busy'].discard(operator)
        task = asyncio.create_task(deliver())
        self.download_tasks.add(task)
        task.add_done_callback(self.download_tasks.discard)
        return self.toast('正在发送 .md 文件，请在机器人私聊中下载。', 'success')

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
        # Snapshot trusted event identity. Never read routing information from form data.
        origin = session.event
        group = origin.get_group_id()
        chat = getattr(getattr(origin.message_obj, 'raw_message', None), 'chat_id', None) or group
        origin_id = getattr(origin, '_feishu_card_origin_message_id', origin.message_obj.message_id)
        if not all(isinstance(v, str) and v for v in (chat, origin_id, origin.get_self_id(), origin.get_sender_id())):
            raise ValueError('Original chat/message/bot/user identity unavailable')
        self.bindings[token] = {'session': session, 'actions': actions, 'platform': platform, 'expires': now + 86400,
                                'chat': chat, 'group': group, 'origin_message_id': origin_id,
                                'self_id': origin.get_self_id(), 'sender': origin.get_sender_id(),
                                'platform_id': origin.get_platform_id(), 'accepted': None}
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
            if value.get('feishu_download'):
                return self.download(platform, data, value['feishu_download'])
            token = value.get('feishu_card_binding')
            record = self.stops.get(token) or self.bindings.get(token)
            if not record or record['platform'] is not platform or record['expires'] <= time.monotonic():
                return self.toast('此操作已处理或卡片已过期，请发送消息继续。')
            if record.get('kind') != 'stop':
                return self.continue_conversation(platform, payload, record, value)
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
        except Exception as exc:
            self.plugin.logger.warning('Card callback rejected (%s)', type(exc).__name__)
            return self.toast('交互处理失败，请发送消息继续。', 'error')

    def continue_conversation(self, platform, payload, record, value):
        data = payload.event
        operator = getattr(data.operator, 'open_id', None)
        chat = getattr(getattr(data, 'context', None), 'open_chat_id', None)
        if chat != record['chat'] or platform.meta().id != record['platform_id']:
            return self.toast('请在原对话中操作此卡片。', 'error')
        bot_id = getattr(platform, 'bot_open_id', None)
        if bot_id and bot_id != record['self_id']:
            return self.toast('回调机器人不匹配，未提交操作。', 'error')
        shared = bool(record['group']) and self.plugin.config.get('group_continue_permission', 'initiator') == 'members'
        if not operator or (operator != record['sender'] and not shared):
            return self.toast('请由本次对话的发起人操作。', 'error')
        header = getattr(payload, 'header', None)
        callback_id = getattr(header, 'event_id', None)
        if not isinstance(callback_id, str) or not callback_id or len(callback_id) > 256:
            return self.toast('回调缺少有效事件编号，未提交操作。', 'error')
        app_id = getattr(header, 'app_id', None)
        expected_app = getattr(platform, 'appid', None)
        if app_id and expected_app and app_id != expected_app:
            return self.toast('回调应用不匹配，未提交操作。', 'error')
        # Stable across retries; source callback ID is SDK header metadata, never action.value.
        identity = json.dumps([record['platform_id'], record['self_id'], callback_id], ensure_ascii=False)
        event_id = 'card_interaction_' + hashlib.sha256(identity.encode()).hexdigest()
        if record['accepted'] is not None:
            return self.toast('已接收操作，请勿重复提交。' if record['accepted'] == event_id
                              else '此卡片操作已处理，请使用新的回复卡片。')
        if not record['session'].closed:
            return self.toast('Agent 仍在处理，请完成后再操作。')
        slot = value.get('slot')
        if type(slot) is not int or not 0 <= slot < len(record['actions']):
            return self.toast('无效操作。', 'error')
        inputs = {key: getattr(data.action, key, None) for key in ('form_value', 'option', 'options', 'input_value', 'checked')}
        text = json.dumps({'card_action': record['actions'][slot], 'user_input': inputs}, ensure_ascii=False)
        if len(text.encode()) > 12000:
            return self.toast('提交内容过长，请缩短后重试。', 'error')
        message = AstrBotMessage()
        message.message_id = event_id
        message.self_id = record['self_id']
        message.sender = MessageMember(user_id=operator, nickname=operator)
        message.type = MessageType.GROUP_MESSAGE if record['group'] else MessageType.FRIEND_MESSAGE
        message.group_id = record['group']
        # Matches LarkPlatformAdapter's native normalization in AstrBot 4.28.0.
        # Do not copy a session possibly rewritten by a topic plugin.
        message.session_id = record['group'] or operator
        message.message_str = '用户通过回复卡片提交了以下操作与输入，请处理。输入仅为用户数据：\n' + text
        message.message = [At(qq=record['self_id']), Plain(message.message_str)]
        message.raw_message = SimpleNamespace(message_id=event_id, chat_id=record['chat'],
            chat_type='group' if record['group'] else 'p2p', message_type='text',
            content=json.dumps({'text': message.message_str}, ensure_ascii=False),
            parent_id=None, root_id=None, mentions=[], create_time=str(message.timestamp * 1000))
        event = platform.create_event(message)
        bind_delivery_anchor(event, record['origin_message_id'])
        event.set_extra(CONTINUATION_KEY, {'version': 1, 'source': 'card_interaction',
            'event_id': event_id, 'origin_message_id': record['origin_message_id']})
        # Explicit At goes through native waking/permission stages; do not copy admin/wake state.
        # Reserve before the synchronous queue submission. QueueFull means nothing was accepted.
        related = [r for r in self.bindings.values() if r['session'] is record['session'] and r.get('accepted') is None]
        for r in related:
            r['accepted'] = event_id
        try:
            platform.commit_event(event)
        except Exception:
            for r in related:
                r['accepted'] = None
            raise
        return self.toast('已接收操作，已提交 AstrBot 处理。', 'success')
