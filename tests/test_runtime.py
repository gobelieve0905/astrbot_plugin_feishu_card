"""Run with the installed AstrBot venv. Network-free delivery/lifecycle regressions."""
import asyncio
import copy
import importlib
import itertools
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
package = Path(__file__).resolve().parents[1].name
session_module = importlib.import_module(package + '.session')
compat = importlib.import_module(package + '.compat')
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Json


class FakeTransport:
    ids = itertools.count()
    def __init__(self, bot):
        self.bodies = []
        self.fail = False
        self.files = []
        self.file_fail = False

    async def reply_file(self, content, filename, message_id):
        if self.file_fail:
            raise RuntimeError("file unavailable")
        self.files.append((content, filename, message_id))

    async def create(self, body):
        return 'test-card-' + str(next(self.ids))

    async def update(self, card_id, body, sequence):
        if self.fail:
            raise RuntimeError('test outage')
        self.bodies.append(body)


class FakeEvent:
    message_str = 'test request'
    message_obj = SimpleNamespace(message_id='test-only')
    bot = None

    def __init__(self):
        self.message_obj = SimpleNamespace(message_id='test-only', raw_message=SimpleNamespace(chat_id='test-chat'))
        self.sent = []
        self.native = []
        self.extras = {}
        self.session = SimpleNamespace(session_id='test-session')

    def is_stopped(self):
        return False

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_platform_id(self):
        return 'test-platform'

    def get_group_id(self):
        return getattr(self, "group_id", "")

    def get_self_id(self):
        return "test-bot"

    def get_sender_id(self):
        return 'test-user'


    async def send(self, chain):
        self.native.append(chain)

    async def send_streaming(self, generator, use_fallback=False):
        async for chain in generator:
            self.native.append(chain)

    async def _send_card_message(self, card, **kwargs):
        self.sent.append(card)
        return True


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import logging
        self.original_transport = session_module.Transport
        session_module.Transport = FakeTransport
        self.plugin = SimpleNamespace(config={}, sessions=set(), remember=lambda *x: None,
                                      forget=lambda *x: None, logger=logging.getLogger('test'))

    async def asyncTearDown(self):
        for session in list(self.plugin.sessions):
            await session.finish()
        session_module.Transport = self.original_transport

    async def new_session(self):
        event = FakeEvent()
        session = session_module.Session(self.plugin, event)
        self.plugin.sessions.add(session)
        await session.start()
        return event, session

    async def test_download_is_only_sent_on_viewer_click(self):
        from unittest.mock import AsyncMock
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger
        interactions = importlib.import_module(package + '.interactions')
        manager = interactions.Interactions(self.plugin)
        platform = SimpleNamespace(meta=lambda: SimpleNamespace(id='test-platform'))
        manager.installed = [(platform, {}, None)]
        manager.loop = asyncio.get_running_loop()
        manager.install = lambda: None
        self.plugin.interactions = manager
        event, session = await self.new_session()
        session.transport.send_download = AsyncMock()
        session.done_received = True
        session.final_text = 'full reply'
        await session.finish()
        session.transport.send_download.assert_not_awaited()
        value = session.state.download_value
        self.assertTrue(value)
        def click(user):
            return P2CardActionTrigger({'event': {'operator': {'open_id': user},
                'context': {'open_chat_id': 'forwarded-chat'}, 'action': {'value': value}}})
        self.assertIn('私聊', manager.receive(platform, click('other-viewer')).toast.content)
        manager.receive(platform, click('other-viewer'))
        await asyncio.gather(*list(manager.download_tasks))
        session.transport.send_download.assert_awaited_once_with(b'full reply', 'other-viewer')
        manager.receive(platform, click('another-viewer'))
        await asyncio.gather(*list(manager.download_tasks))
        self.assertEqual(session.transport.send_download.await_count, 2)
        self.plugin.config['enable_reply_download'] = False
        self.assertIn('关闭', manager.receive(platform, click('third-viewer')).toast.content)
        event2, session2 = await self.new_session()
        session2.done_received = True
        await session2.finish()
        self.assertIsNone(session2.state.download_value)
        self.assertEqual(session.transport.files, [])
        manager.close()
        self.plugin.interactions = SimpleNamespace(release_stop=lambda s: None)

    async def test_first_card_before_model_and_single_delivery(self):
        event, session = await self.new_session()
        self.assertEqual(len(event.sent), 1)
        async def stream():
            yield MessageChain(type='reasoning').message('PRIVATE REASONING')
            yield MessageChain().message('hello')
            yield MessageChain(type='break')
            yield MessageChain().message('world')
            session.done_received = True
        await event.send_streaming(stream())
        self.assertEqual(session.state.text, 'world')
        self.assertEqual(session.state.narratives, ['hello'])
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(event.native, [])
        self.assertEqual(session.state.terminal, '已完成')
        self.assertNotIn('send_streaming', event.__dict__)
        self.assertTrue(session.task.done())

    async def test_failure_not_completed_or_raw_error(self):
        event, session = await self.new_session()
        async def stream():
            session.failed = True
            yield MessageChain().message('raw secret provider exception')
        await event.send_streaming(stream())
        self.assertEqual(session.state.terminal, '本轮未完成')
        self.assertNotIn('secret', session.state.text)

    async def test_update_outage_sends_body(self):
        event, session = await self.new_session()
        session.transport.fail = True
        session.done_received = True
        await event.send(MessageChain().message('final text'))
        self.assertEqual(len(event.native), 1)
        self.assertIn('final text', event.native[0].get_plain_text())

    async def test_concurrent_isolation(self):
        first, a = await self.new_session()
        second, b = await self.new_session()
        a.done_received = b.done_received = True
        await asyncio.gather(first.send(MessageChain().message('first')), second.send(MessageChain().message('second')))
        self.assertEqual(a.state.text, 'first')
        self.assertEqual(b.state.text, 'second')

    async def test_unload_restores_native_and_cleans_task(self):
        event, session = await self.new_session()
        await session.finish('插件已停用')
        await event.send(MessageChain().message('native'))
        self.assertEqual(len(event.native), 1)
        self.assertTrue(session.task.done())

    async def test_disable_during_stream_delegates_remaining(self):
        event, session = await self.new_session()
        async def stream():
            yield MessageChain().message('before')
            await session.finish('插件已停用')
            yield MessageChain().message('after')
        await event.send_streaming(stream())
        self.assertEqual(session.state.text, 'before')
        self.assertEqual(event.native[0].get_plain_text(), 'after')

    async def test_stream_exception_is_terminal_without_raw_exception(self):
        event, session = await self.new_session()
        async def stream():
            yield MessageChain().message('partial')
            raise RuntimeError('sensitive upstream body')
        await event.send_streaming(stream())
        self.assertEqual(session.state.terminal, '本轮未完成')
        self.assertEqual(session.state.text, 'partial')
        self.assertEqual(event.native, [])

    async def test_real_fallback_error_marks_card_failed(self):
        from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        event, session = await self.new_session()
        event.get_extra = lambda key: session
        async def bad_model(**kwargs):
            raise RuntimeError('simulated provider outage')
            yield
        runner = SimpleNamespace(
            run_context=SimpleNamespace(messages=[1], context=SimpleNamespace(event=event)),
            provider=SimpleNamespace(provider_config={'id': 'test-provider'}), fallback_providers=[],
            _is_stop_requested=lambda: False, EMPTY_OUTPUT_RETRY_ATTEMPTS=1,
            EMPTY_OUTPUT_RETRY_WAIT_MIN_S=0.1, EMPTY_OUTPUT_RETRY_WAIT_MAX_S=0.1,
            _iter_llm_responses=bad_model)
        observer = compat.Observer()
        try:
            observer.install()
            responses = [resp async for resp in ToolLoopAgentRunner._iter_llm_responses_with_fallback(runner)]
            self.assertEqual(responses[-1].role, 'err')
            self.assertTrue(session.failed)
            self.assertTrue(session.done_received)
            self.assertEqual(session.state.terminal, '本轮未完成')
        finally:
            observer.uninstall()

    async def test_missing_agent_terminal_not_claimed_complete(self):
        event, session = await self.new_session()
        async def stream():
            yield MessageChain().message('partial without host terminal')
        await event.send_streaming(stream())
        self.assertEqual(session.state.terminal, '本轮未完成')

    async def test_tool_status_does_not_enter_answer(self):
        event, session = await self.new_session()
        await event.send(MessageChain(type='tool_call').message('host tool status'))
        await event.send(MessageChain(type='tool_call_result').message('raw tool result'))
        self.assertEqual(session.state.text, '')
        session.done_received = True
        await event.send(MessageChain().message('final answer'))
        self.assertEqual(session.state.text, 'final answer')

    async def test_authoritative_final_replaces_interim_text(self):
        event, session = await self.new_session()
        async def stream():
            yield MessageChain().message('public plan')
            session.archive_progress()  # Tool hook works even with show_tool_use off.
            yield MessageChain().message('partial final')
            session.final_text = 'complete final answer'
            session.done_received = True
        await event.send_streaming(stream())
        self.assertEqual(session.state.text, 'complete final answer')
        self.assertEqual(session.state.narratives, ['public plan'])

    async def test_native_card_survives_final_text_and_failure_fallback(self):
        event, session = await self.new_session()
        card = {'schema': '2.0', 'body': {'elements': [{'tag': 'markdown', 'content': '```python\nprint(1)\n```'}]}}
        await session.present(card, 'Complete fallback')
        session.final_text = 'Card is ready'
        session.done_received = True
        await session.finish()
        self.assertEqual(session.state.text, 'Complete fallback')
        self.assertEqual(session.transport.bodies[-1]['body']['elements'][0]['content'], card['body']['elements'][0]['content'])
        event, session = await self.new_session()
        await session.present(card, 'Complete fallback')
        session.transport.fail = True
        session.done_received = True
        await session.finish()
        self.assertIn('Complete fallback', event.native[0].get_plain_text())

    async def test_callback_continues_conversation_in_new_card_without_changing_original(self):
        from lark_oapi import EventDispatcherHandler
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger
        from copy import deepcopy
        interactions = importlib.import_module(package + '.interactions')
        rich = importlib.import_module(package + '.rich')
        event, session = await self.new_session()
        queue = []
        def create_event(message):
            e = FakeEvent()
            e.message_obj = message
            e.message_str = message.message_str
            return e
        platform = SimpleNamespace(connection_mode='socket', event_handler=EventDispatcherHandler.builder('', '').build(),
            meta=lambda: SimpleNamespace(id='test-platform'), create_event=create_event, commit_event=queue.append)
        self.plugin.context = SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda: [platform]))
        manager = interactions.Interactions(self.plugin)
        try:
            card = deepcopy(rich.RECIPES['button'])
            token = manager.bind(card, session)
            value = card['body']['elements'][0]['behaviors'][0]['value']
            def payload(user):
                return P2CardActionTrigger({'header': {'event_id': 'test-callback'}, 'event': {'context': {'open_chat_id': 'test-chat'}, 'operator': {'open_id': user}, 'action': {'value': value, 'form_value': {'input': 'test'}}}})
            self.assertIn('发起人', manager.receive(platform, payload('someone-else')).toast.content)
            self.assertIn('仍在处理', manager.receive(platform, payload('test-user')).toast.content)
            session.done_received = True
            await session.finish()
            self.assertIn('已接收操作', manager.receive(platform, payload('test-user')).toast.content)
            self.assertEqual(len(queue), 1)
            manager.receive(platform, payload('test-user'))
            self.assertEqual(len(queue), 1)
            original_bodies = deepcopy(session.transport.bodies)
            original_sequences = list(session.sequences)
            self.assertEqual(queue[0].session, event.session)
            resumed = session_module.Session(self.plugin, queue[0])
            self.assertEqual(resumed.cards, [])
            await resumed.start()
            self.assertEqual(len(queue[0].sent), 1)
            self.assertTrue(set(resumed.cards).isdisjoint(session.cards))
            await resumed.finish('test completed')
            self.assertEqual(session.transport.bodies, original_bodies)
            self.assertEqual(session.sequences, original_sequences)
        finally:
            manager.close()
        self.assertNotIn('p2.card.action.trigger', platform.event_handler._callback_processor_map)

    async def test_standard_tool_failure_and_empty_model_answer(self):
        from mcp.types import CallToolResult, TextContent
        main = importlib.import_module(package + '.main')
        plugin = object.__new__(main.FeishuAgentCard)
        event, session = await self.new_session()
        event.set_extra(compat.KEY, session)
        tool = SimpleNamespace(name='independent_tool', display_name='海外账户 / 花费日报')
        await plugin.tool_start(event, tool, {})
        result = CallToolResult(isError=True, content=[TextContent(type='text', text='private tool details')])
        await plugin.tool_end(event, tool, {}, result)
        self.assertEqual(session.state.tools[0]['name'], '海外账户 / 花费日报')
        self.assertEqual(session.state.tools[0]['status'], '失败')
        self.assertFalse(session.state.sources)
        await plugin.done(event, None, SimpleNamespace(completion_text='', reasoning_content='private reasoning', role='assistant'))
        await session.finish()
        self.assertEqual(session.state.terminal, '未生成正文')
        self.assertIn('海外账户 / 花费日报', session.state.text)
        self.assertNotIn('HTTP', session.state.text)
        self.assertNotIn('private', str(session.transport.bodies))

    async def test_tool_labels_do_not_change_routing_or_unknown_result_semantics(self):
        from mcp.types import CallToolResult, TextContent
        tool = SimpleNamespace(name='stable_id', display_name='账户名 / 操作名')
        result = CallToolResult(content=[TextContent(type='text', text='{"ok":false,"status":400}')])
        self.assertEqual(compat.tool_display_name(tool), '账户名 / 操作名')
        self.assertEqual(tool.name, 'stable_id')
        self.assertFalse(any(name == 'astrbot_plugin_api_import' or name.startswith('astrbot_plugin_api_import.') or name.startswith('data.plugins.astrbot_plugin_api_import') for name in sys.modules))
        self.assertEqual(compat.tool_failure(result), False)
        self.assertEqual(compat.tool_display_name(SimpleNamespace(name='legacy_tool')), 'legacy_tool')

    async def test_native_tools_registered_and_schema_errors_are_returned(self):
        main = importlib.import_module(package + '.main')
        plugin = object.__new__(main.FeishuAgentCard)
        event, session = await self.new_session()
        async def ensure(event): return session
        plugin.ensure = ensure
        result = await plugin.card_render(event, '{bad json', 'fallback')
        self.assertIn('未完成更新', result)

    async def test_stop_callback_cancels_host_work_and_preserves_partial_answer(self):
        from lark_oapi import EventDispatcherHandler
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger
        from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        from astrbot.core.astr_agent_run_util import _watch_agent_stop_signal
        from types import MethodType
        interactions = importlib.import_module(package + '.interactions')
        platform = SimpleNamespace(connection_mode='socket', event_handler=EventDispatcherHandler.builder('', '').build(),
            meta=lambda: SimpleNamespace(id='test-platform'))
        self.plugin.context = SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda: [platform]))
        manager = interactions.Interactions(self.plugin)
        self.plugin.interactions = manager
        try:
            event, session = await self.new_session()
            session.state.text = 'partial answer'
            value = session.state.stop_value
            def payload(user):
                return P2CardActionTrigger({'event': {'context': {'open_chat_id': 'test-chat'}, 'operator': {'open_id': user}, 'action': {'value': value}}})
            self.assertIn('发起人', manager.receive(platform, payload('someone-else')).toast.content)
            self.assertFalse(session.stop_requested)
            runner = SimpleNamespace(_abort_signal=asyncio.Event(), done=lambda: False)
            runner.request_stop = MethodType(ToolLoopAgentRunner.request_stop, runner)
            cancelled = asyncio.Event()
            started = asyncio.Event()
            async def pending_work():
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            work = asyncio.create_task(ToolLoopAgentRunner._await_or_stop(runner, pending_work()))
            await started.wait()
            self.assertIn('已请求终止', manager.receive(platform, payload('test-user')).toast.content)
            self.assertIn('正在终止', manager.receive(platform, payload('test-user')).toast.content)
            await asyncio.wait_for(_watch_agent_stop_signal(runner, event), 1)
            await asyncio.wait_for(work, 1)
            self.assertTrue(cancelled.is_set())
            session.final_text = 'late final answer'
            async def stream():
                yield MessageChain().message('late chunk')
                yield MessageChain(type='aborted')
            await session.stream(stream())
            self.assertEqual(session.state.text, 'partial answer')
            self.assertEqual(session.state.terminal, '已终止')
            self.assertEqual(len(event.sent), 1)
            self.assertFalse(manager.stops)
            self.assertNotIn('stop_answer', str(session.transport.bodies[-1]))
        finally:
            manager.close()

    async def test_group_stop_permission_configuration(self):
        from lark_oapi import EventDispatcherHandler
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger
        interactions = importlib.import_module(package + '.interactions')
        platform = SimpleNamespace(connection_mode='socket', event_handler=EventDispatcherHandler.builder('', '').build(),
            meta=lambda: SimpleNamespace(id='test-platform'))
        self.plugin.context = SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda: [platform]))
        manager = interactions.Interactions(self.plugin)
        self.plugin.interactions = manager
        try:
            event, session = await self.new_session()
            event.group_id = 'test-group'
            event.message_obj.raw_message.chat_id = 'test-group'
            value = session.state.stop_value
            def click(user, chat='test-group', callback=value):
                return manager.receive(platform, P2CardActionTrigger({'event': {
                    'operator': {'open_id': user}, 'context': {'open_chat_id': chat}, 'action': {'value': callback}}}))
            self.assertIn('发起人', click('other-member').toast.content)
            self.plugin.config['group_stop_initiator_only'] = False
            self.assertIn('发起人', click('other-member', 'other-group').toast.content)
            self.assertIn('发起人', click('other-member', None).toast.content)
            event.group_id = ''
            self.assertIn('发起人', click('other-member').toast.content)
            self.assertFalse(session.stop_requested)
            event.group_id = 'test-group'
            # Allowing group stops must not grant access to form/continuation actions.
            manager.bindings['form-test'] = {'session': session, 'platform': platform, 'expires': float('inf'), 'actions': [{}], 'chat': 'test-group', 'platform_id': 'test-platform', 'group': 'test-group', 'sender': 'test-user'}
            self.assertIn('发起人', click('other-member', callback={'feishu_card_binding': 'form-test', 'slot': 0}).toast.content)
            self.assertIn('已请求终止', click('other-member').toast.content)
            self.assertTrue(session.stop_requested)
            self.assertTrue(event.get_extra('agent_stop_requested'))
            await session.finish()
        finally:
            manager.close()

    def continuation_fixture(self, group='group-one', origin='question-one'):
        from astrbot.core.platform.sources.lark.lark_adapter import LarkPlatformAdapter
        from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
        from astrbot.core.platform.message_type import MessageType
        from lark_oapi import EventDispatcherHandler
        from types import MethodType
        interactions = importlib.import_module(package + '.interactions')
        platform = SimpleNamespace(connection_mode='socket', appid='test-app', bot_open_id='test-bot',
            lark_api=None, _event_queue=asyncio.Queue(),
            event_handler=EventDispatcherHandler.builder('', '').build(),
            meta=lambda: SimpleNamespace(id='test-platform', name='lark'))
        platform.create_event = MethodType(LarkPlatformAdapter.create_event, platform)
        platform.commit_event = MethodType(LarkPlatformAdapter.commit_event, platform)
        msg = AstrBotMessage()
        msg.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
        msg.group_id = group
        msg.sender = MessageMember('owner', 'original nickname')
        msg.self_id = 'test-bot'
        msg.session_id = group or 'owner'
        msg.message_id = origin
        msg.message_str = 'original question'
        msg.message = []
        msg.raw_message = SimpleNamespace(chat_id=group or 'private-chat')
        event = platform.create_event(msg)
        # Emulate another plugin having routed the original event. Do not copy this routing.
        event.session.session_id = 'a-topic-modified-session'
        event.role = 'admin'
        session = SimpleNamespace(event=event, closed=True)
        self.plugin.context = SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda: [platform]))
        manager = interactions.Interactions(self.plugin)
        card = {'schema': '2.0', 'body': {'elements': [{'tag': 'button', 'behaviors': [
            {'type': 'callback', 'value': {'action': 'continue', 'origin_message_id': 'forged'}}]}]}}
        token = manager.bind(card, session)
        return manager, platform, session, {'feishu_card_binding': token, 'slot': 0}

    def continuation_payload(self, value, event_id='callback-one', operator='owner', chat='group-one', **form):
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger
        return P2CardActionTrigger({'header': {'event_id': event_id, 'app_id': 'test-app'}, 'event': {
            'operator': {'open_id': operator}, 'context': {'open_chat_id': chat},
            'action': {'value': value, 'form_value': form}}})

    async def test_continuation_standard_queue_identity_and_contract(self):
        manager, platform, session, value = self.continuation_fixture()
        try:
            self.plugin.config['group_continue_permission'] = 'members'
            value.update(event_id='forged', origin_message_id='forged', chat_id='evil', sender='owner')
            payload = self.continuation_payload(value, operator='member-two',
                conversation_continuation_v1={'version': 99, 'origin_message_id': 'evil'}, session_id='evil')
            response = manager.receive(platform, payload)
            self.assertIn('已接收操作', response.toast.content)
            self.assertNotIn('恢复', response.toast.content)
            event = platform._event_queue.get_nowait()
            self.assertNotEqual(event.message_obj.message_id, 'question-one')
            self.assertTrue(event.message_obj.message_id.startswith('card_interaction_'))
            self.assertEqual(event.message_obj.raw_message.message_id, event.message_obj.message_id)
            self.assertEqual(event.get_extra('conversation_continuation_v1'), {
                'version': 1, 'source': 'card_interaction', 'event_id': event.message_obj.message_id,
                'origin_message_id': 'question-one'})
            self.assertEqual(event.get_sender_id(), 'member-two')
            self.assertEqual(event.get_sender_name(), 'member-two')
            self.assertEqual(event.get_self_id(), 'test-bot')
            self.assertEqual(event.get_group_id(), 'group-one')
            self.assertEqual(event.session.session_id, 'group-one')
            self.assertEqual(event.role, 'member')
            self.assertFalse(event.is_wake)
            self.assertEqual(event.message_obj.raw_message.chat_id, 'group-one')
            self.assertIsNone(event.message_obj.raw_message.parent_id)
            self.assertIn('已接收操作', manager.receive(platform, payload).toast.content)
            self.assertTrue(platform._event_queue.empty())
            self.assertIn('已处理', manager.receive(platform, self.continuation_payload(value, event_id='different')).toast.content)
            self.assertTrue(platform._event_queue.empty())
        finally:
            manager.close()

    async def test_continuation_permissions_private_and_cross_chat(self):
        for group in ('group-one', ''):
            manager, platform, session, value = self.continuation_fixture(group)
            chat = group or 'private-chat'
            try:
                self.plugin.config['group_continue_permission'] = 'initiator'
                self.plugin.config['group_stop_initiator_only'] = False
                self.assertIn('发起人', manager.receive(platform, self.continuation_payload(value, operator='other', chat=chat)).toast.content)
                self.plugin.config['group_continue_permission'] = 'members'
                for wrong_chat in ('another-chat', None):
                    self.assertIn('原对话', manager.receive(platform, self.continuation_payload(value, chat=wrong_chat)).toast.content)
                platform.bot_open_id = 'wrong-bot'
                self.assertIn('机器人', manager.receive(platform, self.continuation_payload(value, chat=chat)).toast.content)
                platform.bot_open_id = 'test-bot'
                invalid = self.continuation_payload(value, chat=chat)
                invalid.header.app_id = 'wrong-app'
                self.assertIn('应用', manager.receive(platform, invalid).toast.content)
                self.assertTrue(platform._event_queue.empty())
                if not group:
                    self.assertIn('发起人', manager.receive(platform, self.continuation_payload(value, operator='other', chat=chat)).toast.content)
                self.assertIn('已接收', manager.receive(platform, self.continuation_payload(value, chat=chat)).toast.content)
                event = platform._event_queue.get_nowait()
                self.assertEqual(event.session.session_id, group or 'owner')
                self.assertEqual(event.get_sender_id(), 'owner')
            finally:
                manager.close()

    async def test_continuation_retry_queue_failure_ids_and_expiry(self):
        manager, platform, session, value = self.continuation_fixture()
        try:
            invalid = self.continuation_payload(value, event_id=None)
            self.assertIn('事件编号', manager.receive(platform, invalid).toast.content)
            self.assertTrue(platform._event_queue.empty())
            attempted = []
            original_commit = platform.commit_event
            def fail(event):
                attempted.append(event.message_obj.message_id)
                raise asyncio.QueueFull()
            platform.commit_event = fail
            payload = self.continuation_payload(value)
            self.assertIn('失败', manager.receive(platform, payload).toast.content)
            platform.commit_event = original_commit
            manager.receive(platform, payload)
            first = platform._event_queue.get_nowait()
            self.assertEqual(first.message_obj.message_id, attempted[0])
            # Different original card / genuine callback gets a distinct event ID.
            from copy import deepcopy
            rich = importlib.import_module(package + '.rich')
            other_event = copy.copy(session.event)
            other_event.message_obj = copy.deepcopy(session.event.message_obj)
            other_event.message_obj.message_id = 'question-two'
            other = SimpleNamespace(event=other_event, closed=True)
            card = deepcopy(rich.RECIPES['button'])
            manager.bind(card, other)
            other_value = card['body']['elements'][0]['behaviors'][0]['value']
            manager.receive(platform, self.continuation_payload(other_value, event_id='callback-two'))
            second = platform._event_queue.get_nowait()
            self.assertNotEqual(first.message_obj.message_id, second.message_obj.message_id)
            self.assertEqual(second.get_extra('conversation_continuation_v1')['origin_message_id'], 'question-two')
            self.assertEqual(first.get_extra('conversation_continuation_v1')['origin_message_id'], 'question-one')
            manager.close()
            self.assertIn('过期', manager.receive(platform, payload).toast.content)
            self.assertTrue(platform._event_queue.empty())
        finally:
            manager.close()

    async def test_continuation_delivery_preserves_synthetic_id_with_native_fallback(self):
        from unittest.mock import AsyncMock, patch
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent
        manager, platform, session, value = self.continuation_fixture()
        try:
            manager.receive(platform, self.continuation_payload(value))
            event = platform._event_queue.get_nowait()
            new_id = event.message_obj.message_id
            with patch.object(LarkMessageEvent, 'send_message_chain', new_callable=AsyncMock) as send_chain, \
                 patch.object(LarkMessageEvent, '_send_im_message', new_callable=AsyncMock, return_value=True) as send_card:
                await event.send(MessageChain().message('native fallback'))
                self.assertEqual(send_chain.call_args.kwargs['reply_message_id'], 'question-one')
                await event._send_card_message('card', reply_message_id=new_id)
                self.assertEqual(send_card.call_args.kwargs['reply_message_id'], 'question-one')
                self.assertEqual(event.message_obj.message_id, new_id)
            # Session wrappers restore the delivery wrappers, not the broken synthetic reply ID.
            resumed = session_module.Session(self.plugin, event)
            with patch.object(LarkMessageEvent, '_send_im_message', new_callable=AsyncMock, return_value=True):
                await resumed.start()
                resumed.done_received = True
                await resumed.finish()
            with patch.object(LarkMessageEvent, 'send_message_chain', new_callable=AsyncMock) as send_chain:
                await event.send(MessageChain().message('after restoration'))
                self.assertEqual(send_chain.call_args.kwargs['reply_message_id'], 'question-one')
            async def chunks():
                yield MessageChain().message('native stream fallback')
            with patch.object(LarkMessageEvent, '_create_streaming_card', new_callable=AsyncMock, return_value=None), \
                 patch.object(LarkMessageEvent, 'send_message_chain', new_callable=AsyncMock) as send_chain:
                await event.send_streaming(chunks())
                self.assertEqual(send_chain.call_args.kwargs['reply_message_id'], 'question-one')
            self.assertEqual(event.message_obj.message_id, new_id)
        finally:
            manager.close()

    async def test_continuation_close_does_not_reinstall_during_finalization(self):
        manager, platform, session, value = self.continuation_fixture()
        manager.close()
        manager.install()
        self.assertNotIn('p2.card.action.trigger', platform.event_handler._callback_processor_map)
        rich = importlib.import_module(package + '.rich')
        with self.assertRaises(ValueError):
            manager.bind(copy.deepcopy(rich.RECIPES['button']), session)
        with self.assertRaises(ValueError):
            manager.bind_download(session, b'test')
        self.assertEqual(manager.bindings, {})
        self.assertNotIn('p2.card.action.trigger', platform.event_handler._callback_processor_map)
        self.assertIn('过期', manager.receive(platform, self.continuation_payload(value)).toast.content)

    async def test_interaction_notice_and_one_submission_across_members(self):
        manager, platform, session, value = self.continuation_fixture()
        rich = importlib.import_module(package + '.rich')
        card_module = importlib.import_module(package + '.card')
        interactions = importlib.import_module(package + '.interactions')
        try:
            self.plugin.config['group_continue_permission'] = 'members'
            form = copy.deepcopy(rich.RECIPES['form'])
            manager.bind(form, session)
            notice = form['body']['elements'][-1]
            self.assertEqual(notice['content'], interactions.INTERACTION_NOTICE)
            state = card_module.State(rich_card=form, text='test', terminal='已完成')
            rendered = card_module.render(state, {'show_process': False, 'show_sources': False}, 'test')
            self.assertIn(notice, rendered['body']['elements'])
            self.assertIn('24 小时', notice['content'])
            self.assertIn('重载或重启', notice['content'])
            self.assertIn('所有人合计', notice['content'])
            form_value = form['body']['elements'][0]['elements'][1]['behaviors'][0]['value']
            self.assertIn('已接收', manager.receive(platform, self.continuation_payload(form_value, operator='member-one')).toast.content)
            self.assertIn('已处理', manager.receive(platform, self.continuation_payload(form_value, operator='member-two', event_id='second')).toast.content)
            self.assertEqual(platform._event_queue.qsize(), 1)
            plain = copy.deepcopy(rich.RECIPES['code'])
            original = copy.deepcopy(plain)
            self.assertIsNone(manager.bind(plain, session))
            self.assertEqual(plain, original)
        finally:
            manager.close()

    async def test_platform_selector_filters_and_preserves_selected_scope(self):
        main = importlib.import_module(package + '.main')
        class Config(dict):
            schema = {'platform_ids': {'type': 'list', 'options': []}}
        config = Config(platform_ids=['removed-instance'])
        inventory = {'platform': [{'type': 'lark', 'id': 'feishu-b', 'enable': False},
            {'type': 'lark', 'id': 'feishu-a', 'app_secret': 'never-expose'},
            {'type': 'qq', 'id': 'not-feishu'}, {'type': 'lark', 'id': None}]}
        plugin = object.__new__(main.FeishuAgentCard)
        plugin._dashboard_config = config
        plugin.context = SimpleNamespace(get_config=lambda: inventory)
        plugin.refresh_platform_options()
        self.assertEqual(config.schema['platform_ids']['options'], ['feishu-a', 'feishu-b', 'removed-instance'])
        self.assertEqual(config['platform_ids'], ['removed-instance'])
        self.assertNotIn('never-expose', str(config.schema))
        inventory['platform'] = [{'type': 'lark', 'id': 'new-instance'}]
        plugin.refresh_platform_options()
        self.assertEqual(config.schema['platform_ids']['options'], ['new-instance', 'removed-instance'])

    async def test_direct_text_uses_card_and_final_answer(self):
        from unittest.mock import AsyncMock, patch
        from astrbot.core.tools.message_tools import SendMessageToUserTool
        original = AsyncMock(return_value="native")
        with patch.object(SendMessageToUserTool, "call", original):
            observer = compat.Observer()
            try:
                observer.install()
                event, session = await self.new_session()
                event.unified_msg_origin = "test:LARK:test-session"
                event.set_extra(compat.KEY, session)
                context = SimpleNamespace(context=SimpleNamespace(event=event))
                tool = SendMessageToUserTool()
                args = {"messages": [{"type": "plain", "text": "API failed"}]}
                await tool.call(context, **args)
                await tool.call(context, session=event.unified_msg_origin, **args)
                self.assertEqual(session.direct_texts, ["API failed"])
                self.assertEqual(session.state.text, "API failed")
                session.final_text = "Final explanation"
                await session.finish()
                self.assertEqual(session.state.text, "Final explanation")
                original.assert_not_awaited()
                self.assertEqual(event.native, [])
            finally:
                observer.uninstall()
            self.assertIs(SendMessageToUserTool.call, original)

    async def test_direct_text_scope_and_tool_only_answer(self):
        from unittest.mock import AsyncMock, patch
        from astrbot.core.tools.message_tools import SendMessageToUserTool
        original = AsyncMock(return_value="native")
        with patch.object(SendMessageToUserTool, "call", original):
            observer = compat.Observer()
            try:
                observer.install()
                event, session = await self.new_session()
                event.unified_msg_origin = "test:LARK:test-session"
                event.set_extra(compat.KEY, session)
                context = SimpleNamespace(context=SimpleNamespace(event=event))
                tool = SendMessageToUserTool()
                text = [{"type": "plain", "text": "Useful answer"}]
                await tool.call(context, messages=text, session="other:LARK:other")
                await tool.call(context, messages=[*text, {"type": "image", "url": "test"}])
                inactive = SimpleNamespace(context=SimpleNamespace(event=FakeEvent()))
                await tool.call(inactive, messages=text)
                self.assertEqual(original.await_count, 3)
                await tool.call(context, messages=text)
                session.state.text = ""  # Host starts another model response without a final body.
                await session.finish()
                self.assertEqual(session.state.text, "Useful answer")
                self.assertEqual(event.native, [])
            finally:
                observer.uninstall()

    async def test_observer_restore(self):
        from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        original = ToolLoopAgentRunner._iter_llm_responses
        outer = ToolLoopAgentRunner._iter_llm_responses_with_fallback
        observer = compat.Observer()
        try:
            observer.install()
            self.assertIsNot(original, ToolLoopAgentRunner._iter_llm_responses)
        finally:
            observer.uninstall()
        self.assertIs(original, ToolLoopAgentRunner._iter_llm_responses)
        self.assertIs(outer, ToolLoopAgentRunner._iter_llm_responses_with_fallback)


if __name__ == '__main__':
    unittest.main()
