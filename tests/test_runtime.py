"""Run with the installed AstrBot venv. Network-free delivery/lifecycle regressions."""
import asyncio
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
        self.sent = []
        self.native = []
        self.extras = {}
        self.session = SimpleNamespace(session_id='test-session')

    def is_stopped(self):
        return False

    def get_extra(self, key):
        return self.extras.get(key)

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
                return P2CardActionTrigger({'event': {'operator': {'open_id': user}, 'action': {'value': value, 'form_value': {'input': 'test'}}}})
            self.assertIn('发起人', manager.receive(platform, payload('someone-else')).toast.content)
            self.assertIn('仍在处理', manager.receive(platform, payload('test-user')).toast.content)
            session.done_received = True
            await session.finish()
            self.assertIn('已收到', manager.receive(platform, payload('test-user')).toast.content)
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
                return P2CardActionTrigger({'event': {'operator': {'open_id': user}, 'action': {'value': value}}})
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
            manager.bindings['form-test'] = {'session': session, 'platform': platform, 'expires': float('inf'), 'actions': [{}]}
            self.assertIn('发起人', click('other-member', callback={'feishu_card_binding': 'form-test', 'slot': 0}).toast.content)
            self.assertIn('已请求终止', click('other-member').toast.content)
            self.assertTrue(session.stop_requested)
            self.assertTrue(event.get_extra('agent_stop_requested'))
            await session.finish()
        finally:
            manager.close()

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
