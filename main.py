"""Installable AstrBot Star: Feishu Agent Card, preview 0.1.3."""
import asyncio
import json
import os
import time

from astrbot.api import AstrBotConfig
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools

from .card import State, label, render
from .compat import KEY, Observer
from .session import Session
from .transport import Transport


class FeishuAgentCard(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = dict(config)
        try:
            self.config["model_prices"] = json.loads(config.get("model_prices_json", "{}"))
        except (ValueError, TypeError):
            self.config["model_prices"] = {}
        self.sessions = set()
        self.observer = Observer()
        self.enabled = False
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_feishu_agent_card")
        self.ledger_path = self.data_dir / "pending_cards.json"
        self.pending = {}
        if self.ledger_path.exists():
            self.pending = json.loads(self.ledger_path.read_text())

    async def initialize(self):
        if not self.config.get("enabled", True):
            return
        try:
            self.observer.install()
            self.enabled = True
            self.logger.info("Feishu Agent Card 0.1.3 ready (AstrBot 4.28.0)")
        except Exception as exc:
            self.logger.warning("Feishu Agent Card disabled: %s", str(exc))

    def save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temp = self.ledger_path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(self.pending, file)
        os.replace(temp, self.ledger_path)

    def remember(self, session, card_id, sequence=0):
        self.pending[card_id] = {"platform": session.event.get_platform_id(), "sequence": sequence, "created": time.time()}
        self.save()

    def forget(self, cards):
        for card in cards:
            self.pending.pop(card, None)
        self.save()

    @filter.on_astrbot_loaded()
    async def recover(self):
        # Do not replay questions or persist business text. Close only our own unfinished cards.
        if not self.enabled:
            return
        platforms = self.context.platform_manager.get_insts()
        for platform in platforms:
            bot = getattr(platform, "lark_api", None)
            if bot is None:
                continue
            platform_id = platform.meta().id
            for card_id, record in list(self.pending.items()):
                if record["platform"] != platform_id:
                    continue
                state = State(text="服务已重启，上次任务的最终状态无法确认。请重新发送请求。", terminal="运行已中断")
                try:
                    await Transport(bot).update(card_id, render(state, self.config, state.text), record["sequence"] + 100)
                    self.forget([card_id])
                except Exception as exc:
                    self.logger.warning("Pending card recovery failed (%s)", type(exc).__name__)

    async def ensure(self, event):
        existing = event.get_extra(KEY)
        if existing:
            return existing
        if not self.enabled or not self.config.get("enabled", True):
            return None
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent
        if not isinstance(event, LarkMessageEvent):
            return None
        ids = self.config.get("platform_ids", [])
        if ids and event.get_platform_id() not in ids:
            return None
        group = bool(event.get_group_id())
        if not self.config.get("enable_group" if group else "enable_private", True):
            return None
        if len(self.sessions) >= 64:
            return None
        session = Session(self, event)
        try:
            await session.start()
        except Exception as exc:
            session.restore()
            self.logger.warning("Card start failed; native delivery retained (%s)", type(exc).__name__)
            return None
        event.set_extra(KEY, session)
        self.sessions.add(session)
        owner_task = asyncio.current_task()
        if owner_task:
            def owner_done(_task):
                if not session.closed:
                    asyncio.create_task(session.finish("运行已中断" if not session.done_received else ""))
            owner_task.add_done_callback(owner_done)
        return session

    @filter.on_waiting_llm_request()
    async def waiting(self, event):
        await self.ensure(event)

    @filter.on_llm_request()
    async def request(self, event, req):
        session = await self.ensure(event)
        if session:
            session.state.step("正在准备模型请求")
            presentation = ("飞书卡片展示格式：最终回答请以一行简短、准确概括本次回答主题的 Markdown 二级标题"
                            "（## 主题）开头，随后空行再写正文。不要用‘回答’作为标题，不要重复用户原问题；"
                            "工具调用前的过程说明无需标题。若用户明确指定其他输出格式，则优先遵循用户要求。")
            if presentation not in (req.system_prompt or ""):
                req.system_prompt = (req.system_prompt or "") + "\n\n" + presentation

    @filter.on_agent_begin()
    async def begin(self, event, run_context):
        session = await self.ensure(event)
        if session:
            session.state.step("Agent 已开始处理")

    @filter.on_using_llm_tool()
    async def tool_start(self, event, tool, tool_args):
        session = event.get_extra(KEY)
        if not session or session.closed:
            return
        session.archive_progress()
        name = label(tool.name, 80)
        session.state.tools.append({"name": name, "start": time.monotonic(), "status": "执行中"})
        session.state.step(f"正在调用工具：{name}")

    @filter.on_llm_tool_respond()
    async def tool_end(self, event, tool, tool_args, tool_result):
        session = event.get_extra(KEY)
        if not session or session.closed:
            return
        error = getattr(tool_result, "isError", False)
        name = label(tool.name, 80)
        for record in reversed(session.state.tools):
            if record["name"] == name and "end" not in record:
                record.update(end=time.monotonic(), status="失败" if error else "已返回")
                break
        session.state.step(f"工具{name}已返回，等待模型继续处理" if not error else f"工具{name}失败，等待模型处理")
        # Only explicit structured source fields; arbitrary URLs inside result text aren't citations.
        data = getattr(tool_result, "structuredContent", None)
        if not error:
            session.state.source(name, kind="工具结果")
            if isinstance(data, dict):
                for source in data.get("sources", [])[:20] if isinstance(data.get("sources"), list) else []:
                    if isinstance(source, dict):
                        session.state.source(source.get("title"), source.get("url", ""), source.get("type", "资料"))
        self.read_registered_sources(event, session)

    def read_registered_sources(self, event, session):
        sources = event.get_extra("feishu_card_sources", [])
        if isinstance(sources, list):
            for source in sources[:20]:
                if isinstance(source, dict):
                    session.state.source(source.get("title"), source.get("url", ""), source.get("type", "资料"))

    @filter.on_agent_done()
    async def done(self, event, run_context, resp):
        session = event.get_extra(KEY)
        if not session or session.closed:
            return
        session.done_received = True
        session.final_text = getattr(resp, "completion_text", None)
        session.failed = getattr(resp, "role", "") == "err"
        if session.failed:
            session.state.terminal = "本轮未完成"
            session.state.step("模型未能完成回答，请稍后重试")
        else:
            session.state.step("回答已生成，正在完成交付")
        self.read_registered_sources(event, session)
        # Host stores raw reasoning for result decoration; this plugin never displays it.
        event.set_extra("_llm_reasoning_content", "")

    @filter.after_message_sent()
    async def sent(self, event):
        session = event.get_extra(KEY)
        if session and session.done_received and not session.stream_active:
            await session.finish()

    async def terminate(self):
        self.enabled = False
        self.observer.uninstall()
        await asyncio.gather(*(session.finish("插件已停用，后续回复使用原生方式") for session in list(self.sessions)), return_exceptions=True)
