"""One delivery owner per incoming event, with bounded update tasks."""
import asyncio
import json
import time
from types import MethodType

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Json

from .card import State, pages, render, safe_text
from .transport import Transport


class Session:
    def __init__(self, plugin, event):
        self.plugin, self.event = plugin, event
        self.state = State(question=event.message_str)
        self.transport = Transport(event.bot)
        self.cards, self.sequences, self.sent_bodies = [], [], []
        self.closed = False
        self.stream_active = False
        self.done_received = False
        self.final_text = None
        self.failed = False
        self.delivery_failed = False
        self.task = None
        self.lock = asyncio.Lock()
        self.original_send = event.send
        self.original_stream = event.send_streaming
        self.saved_attrs = {k: event.__dict__.get(k) for k in ("send", "send_streaming")}
        self.present_attrs = {k: k in event.__dict__ for k in self.saved_attrs}
        self.wrappers = {}

    async def start(self):
        await self.flush()
        self.state.step("已收到，等待 Agent 处理")

        async def send(_event, chain):
            await self.send(chain)

        async def stream(_event, generator, use_fallback=False):
            await self.stream(generator)

        self.wrappers = {"send": MethodType(send, self.event), "send_streaming": MethodType(stream, self.event)}
        for name, wrapper in self.wrappers.items():
            setattr(self.event, name, wrapper)
        self.task = asyncio.create_task(self.writer())

    def restore(self):
        for name, wrapper in self.wrappers.items():
            if self.event.__dict__.get(name) is wrapper:
                if self.present_attrs[name]:
                    setattr(self.event, name, self.saved_attrs[name])
                else:
                    self.event.__dict__.pop(name, None)

    async def writer(self):
        try:
            while not self.closed:
                await asyncio.sleep(max(1.0, min(10.0, float(self.plugin.config.get("update_interval", 1.5)))))
                try:
                    await self.flush()
                except Exception as exc:
                    # Keep consuming the model stream; terminal delivery retries separately.
                    self.plugin.logger.warning("Card update deferred (%s)", type(exc).__name__)
        except asyncio.CancelledError:
            pass

    async def flush(self):
        async with self.lock:
            parts = [self.state.text] if self.state.rich_card else pages(self.state.text)
            visible_count = len(parts)
            parts.extend(["过程内容已收起，完整回答请查看前面的回答页。"] * max(0, len(self.cards) - len(parts)))
            for index, part in enumerate(parts):
                if not self.state.terminal and index < visible_count - 1 and index < len(self.sent_bodies) and self.sent_bodies[index]:
                    continue
                body = render(self.state, self.plugin.config, part, index, len(parts), index < len(parts) - 1)
                encoded = json.dumps(body, ensure_ascii=False)
                if index >= len(self.cards):
                    card_id = await self.transport.create(body)
                    self.cards.append(card_id)
                    self.sequences.append(0)
                    self.sent_bodies.append("")
                    self.plugin.remember(self, card_id)
                    sent = await asyncio.wait_for(self.event._send_card_message(
                        card_id, reply_message_id=self.event.message_obj.message_id), 15)
                    if not sent:
                        self.delivery_failed = True
                        raise RuntimeError("card_message_not_sent")
                if self.sent_bodies[index] != encoded:
                    self.sequences[index] += 1
                    await self.transport.update(self.cards[index], body, self.sequences[index])
                    self.plugin.remember(self, self.cards[index], self.sequences[index])
                    self.sent_bodies[index] = encoded

    async def present(self, card, fallback):
        if self.closed:
            raise ValueError("This reply is already closed.")
        # Preflight an unposted card, so schema errors reach the model for correction.
        probe = State(rich_card=card, text=fallback)
        await self.transport.create(render(probe, self.plugin.config, fallback))
        async with self.lock:
            previous = (self.state.rich_card, self.state.text)
            self.state.rich_card, self.state.text = card, fallback
        try:
            await self.flush()
        except Exception:
            async with self.lock:
                self.state.rich_card, self.state.text = previous
            raise

    def archive_progress(self):
        """A host tool boundary identifies public interim text, without keyword parsing."""
        if self.state.rich_card:
            return
        text = self.state.text.strip()
        if text:
            self.state.narratives.append(safe_text(text, 2400))
            self.state.narratives = self.state.narratives[-6:]
            self.state.text = ""

    async def send(self, chain):
        if self.closed:
            # Restored after normal completion; an already captured caller still delegates.
            return await self.original_send(chain)
        kind = getattr(chain, "type", "")
        if kind in {"reasoning", "tool_call", "tool_call_result"}:
            return
        if kind == "tool_direct_result":
            return await self.original_send(chain)
        texts, rest = [], []
        for component in chain.chain:
            if isinstance(component, Plain):
                texts.append(component.text)
            elif isinstance(component, Json) and isinstance(component.data, dict) and component.data.get("type") == "lark_collapsible_panel_reasoning":
                continue
            else:
                rest.append(component)
        if texts and not self.failed and not self.state.rich_card:
            value = "".join(texts)
            if value and value != self.state.text:
                self.state.text += ("\n\n" if self.state.text else "") + value
        if rest:
            await self.original_send(MessageChain(chain=rest))
        self.event._has_send_oper = True
        if self.done_received:
            await self.finish()
        else:
            await self.flush()

    async def stream(self, generator):
        self.stream_active = True
        try:
            async for chain in generator:
                if self.closed:
                    async def remaining():
                        yield chain
                        async for rest_chain in generator:
                            yield rest_chain
                    await self.original_stream(remaining())
                    return
                kind = getattr(chain, "type", "")
                if kind == "aborted":
                    self.state.terminal = "已取消 / 已中断"
                    continue
                if kind in {"reasoning", "agent_stats", "tool_call", "tool_call_result"}:
                    continue
                if kind == "break":
                    self.archive_progress()
                    continue
                rest = []
                for component in chain.chain:
                    if isinstance(component, Plain):
                        if not self.failed and not self.state.rich_card:
                            self.state.text += component.text
                    elif isinstance(component, Json) and isinstance(component.data, dict) and component.data.get("type") == "lark_collapsible_panel_reasoning":
                        continue
                    else:
                        rest.append(component)
                if rest:
                    await self.original_send(MessageChain(chain=rest))
        except asyncio.CancelledError:
            self.state.terminal = "已取消 / 已中断"
            raise
        except Exception as exc:
            self.failed = True
            self.state.terminal = "本轮未完成"
            self.state.step("生成过程中发生异常，请稍后重试")
            self.plugin.logger.warning("Model stream interrupted (%s)", type(exc).__name__)
        finally:
            self.stream_active = False
            await self.finish()

    async def finish(self, status=""):
        if self.closed:
            return
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if not self.failed and self.final_text and not self.state.rich_card:
            self.state.text = self.final_text
        self.state.ended = time.monotonic()
        self.state.terminal = status or self.state.terminal or ("本轮未完成" if self.failed or not self.done_received else "已完成")
        if not self.state.text.strip():
            self.state.text = "本轮未能生成回答，请稍后重试。" if self.failed or not self.done_received else "本轮没有生成可展示的正文。"
        self.state.step(self.state.terminal)
        success = False
        try:
            for attempt in range(2):
                try:
                    await self.flush()
                    success = not self.delivery_failed
                    if success:
                        break
                except Exception as exc:
                    self.plugin.logger.warning("Card finalization failed (%s)", type(exc).__name__)
            if not success:
                for part in pages(self.state.text, 8000):
                    await self.original_send(MessageChain().message(f"{self.state.terminal}（卡片更新失败，正文补发）\n\n{part}"))
            if success:
                self.plugin.forget(self.cards)
            self.event._has_send_oper = True
        finally:
            self.restore()
            self.plugin.sessions.discard(self)
