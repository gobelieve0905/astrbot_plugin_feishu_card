"""Run with the installed AstrBot venv. Network-free delivery/lifecycle regressions."""
import asyncio
import importlib
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
    def __init__(self, bot):
        self.bodies = []
        self.fail = False

    async def create(self, body):
        return 'test-card-' + str(len(self.bodies))

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

    def get_extra(self, key):
        return self.extras.get(key)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_platform_id(self):
        return 'test-platform'

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

    async def test_callback_authorization_one_shot_and_same_card_resume(self):
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
            resumed = session_module.Session(self.plugin, queue[0])
            self.assertEqual(resumed.cards, session.cards)
            await resumed.start()
            self.assertEqual(queue[0].sent, [])
            await resumed.finish('test completed')
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
