"""Installable AstrBot Star: Feishu Agent Card, preview 0.2.9."""
import asyncio
import json
import os
import time
from pathlib import Path

from astrbot.api import AstrBotConfig
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools

from .card import State, label, render
from .compat import KEY, Observer
from .session import Session
from .transport import Transport
from .rich import guide, parse_card
from .interactions import Interactions


class FeishuAgentCard(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = dict(config)
        try:
            self.config["model_prices"] = json.loads(config.get("model_prices_json", "{}"))
        except (ValueError, TypeError):
            self.config["model_prices"] = {}
        self.sessions = set()
        self.interactions = Interactions(self)
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
            self.interactions.install()
            self.logger.info("Feishu Agent Card 0.2.9 ready (AstrBot 4.28.0)")
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
        self.interactions.install()
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
            presentation += (" 复杂结果可调用 feishu_card_guide 查看原生卡片组件示例，使用 feishu_card_render"
                             "呈现代码、表格、图表、图片、多栏、按钮或表单。按场景选择合适组件，不必每次都用卡片工具。"
                             "交互行为 value.action 描述用户点击后希望继续的任务。工具成功后无需重复正文。"
                             "名称、标识符和数据须完整输出，不得用省略号缩写；宽表使用原生 table 组件，由插件设置像素列宽；不要另加逐行明细面板。")
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

    @filter.llm_tool(name="feishu_card_guide")
    async def card_guide(self, event, component: str = "all"):
        """查看飞书原生卡片组件、场景选择规则与可直接修改的 JSON 示例。

        Args:
            component(string): all 或 code/table/chart/image/layout/button/form。
        """
        if not await self.ensure(event):
            return "当前会话没有可用的飞书卡片能力。"
        return guide(component)

    @filter.llm_tool(name="feishu_card_upload_image")
    async def card_upload_image(self, event, image_path: str):
        """上传当前任务已生成的本地图片，返回可用于卡片 img/img_combination 的真实 img_key。只读取管理员允许目录中的 PNG/JPEG/GIF/WebP，不访问远程 URL。

        Args:
            image_path(string): 已生成图片的服务器绝对路径；默认允许 AstrBot data/temp 和 data/plugin_data。
        """
        session = await self.ensure(event)
        if not session or session.closed:
            return "当前会话没有可用的飞书卡片能力。"
        try:
            path = Path(image_path).resolve(strict=True)
            data = self.data_dir.parent.parent
            roots = self.config.get("image_roots", []) or [str(data / 'temp'), str(self.data_dir.parent)]
            if not any(path.is_relative_to(Path(root).resolve()) for root in roots):
                raise ValueError("图片不在允许目录中；可放在 AstrBot data/temp 或 data/plugin_data。")
            if not path.is_file() or not 0 < path.stat().st_size <= 10 * 1024 * 1024:
                raise ValueError("图片必须是 10 MB 以内的普通文件。")
            def read_image():
                with path.open('rb') as file:
                    return file.read(10 * 1024 * 1024 + 1)
            content = await asyncio.to_thread(read_image)
            if len(content) > 10 * 1024 * 1024:
                raise ValueError("图片超过 10 MB。")
            if not (content.startswith(b"\x89PNG\r\n\x1a\n") or content.startswith(b"\xff\xd8\xff")
                    or content.startswith((b"GIF87a", b"GIF89a")) or (content[:4] == b"RIFF" and content[8:12] == b"WEBP")):
                raise ValueError("文件不是支持的图片格式。")
            return json.dumps({"img_key": await session.transport.upload_image(content)}, ensure_ascii=False)
        except Exception as exc:
            return "图片上传失败：" + label(str(exc), 200)

    @filter.llm_tool(name="feishu_card_render")
    async def card_render(self, event, card_json: str, fallback_text: str):
        """根据场景构建或替换当前飞书回复卡片，支持原生 JSON 2.0 组件与嵌套布局。先用 guide 查询用法；按钮表单 callback 的 value.action 描述继续处理任务。成功后不要重复输出正文。

        Args:
            card_json(string): 完整飞书 JSON 2.0 文档字符串，含 schema 和 body.elements；真实资源键，不编造数据。
            fallback_text(string): 卡片无法交付时可直接发送给用户的完整文字结果，含代码和关键数据。
        """
        session = await self.ensure(event)
        if not session or session.closed:
            return "当前会话没有可用的飞书卡片能力。"
        binding = None
        try:
            if not fallback_text.strip() or len(fallback_text.encode()) > 60000:
                raise ValueError("fallback_text must contain the complete readable result within 60 KB.")
            card = parse_card(card_json)
            binding = self.interactions.bind(card, session)
            await session.present(card, fallback_text)
            for key, record in list(self.interactions.bindings.items()):
                if record['session'] is session and key != binding:
                    self.interactions.bindings.pop(key, None)
            return "原生卡片已更新到当前回复。无需再重复输出正文。交互控件需飞书应用已订阅 card.action.trigger。"
        except Exception as exc:
            if binding:
                self.interactions.bindings.pop(binding, None)
            return "卡片未完成更新，请修正后重试或改用普通回答：" + label(str(exc), 300)

    async def terminate(self):
        self.enabled = False
        self.interactions.close()
        self.observer.uninstall()
        await asyncio.gather(*(session.finish("插件已停用，后续回复使用原生方式") for session in list(self.sessions)), return_exceptions=True)
