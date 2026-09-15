"""Version-gated, reversible runtime observation. No installed source is changed."""
import contextvars
import inspect
import logging
import re

from .card import label

KEY = "feishu_agent_card_v1"
CURRENT = contextvars.ContextVar("feishu_card_request", default=None)


class RetryHandler(logging.Handler):
    def emit(self, record):
        session = CURRENT.get()
        if not session or session.closed:
            return
        # Match only mechanical retry metadata; never copy upstream error bodies.
        text = record.getMessage()
        match = re.search(r"retrying \((\d+)/(\d+)\)", text)
        if match:
            session.state.step(f"模型请求暂时失败，正在重试（{match[1]}/{match[2]}）")
            session.state.unknown_usage = True


class Observer:
    def __init__(self):
        self.original = None
        self.wrapper = None
        self.handler = None
        self.outer_original = None
        self.outer_wrapper = None
        self.send_original = None
        self.send_wrapper = None

    def install(self):
        from astrbot import __version__
        from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        if str(__version__).lstrip("v") not in {"4.28.0", "4.28.1"}:
            raise RuntimeError("当前仅验收 AstrBot 4.28.0 / 4.28.1；保留原生回复")
        original = ToolLoopAgentRunner._iter_llm_responses
        if getattr(original, "_feishu_card_observer", False):
            raise RuntimeError("已有飞书卡片观察器")
        if "include_model" not in inspect.signature(original).parameters:
            raise RuntimeError("模型流接口不兼容")

        async def observed(runner, *, include_model=True):
            event = getattr(getattr(runner.run_context, "context", None), "event", None)
            session = event.get_extra(KEY) if event else None
            if session and not session.closed:
                provider_id = str(runner.provider.provider_config.get("id", "模型"))
                model_name = str((getattr(runner.req, "model", None) if include_model else None) or runner.provider.get_model() or "")
                model = label(provider_id if provider_id.endswith("/" + model_name) else provider_id + "/" + model_name, 100)
                session.state.calls += 1
                if session.state.models and session.state.models[-1] != model:
                    session.state.step(f"已切换模型：{model}")
                else:
                    session.state.step(f"正在请求模型：{model}")
                if model not in session.state.models:
                    session.state.models.append(model)
            stream = original(runner, include_model=include_model)
            try:
                while True:
                    token = CURRENT.set(session)
                    try:
                        resp = await anext(stream)
                    except StopAsyncIteration:
                        break
                    finally:
                        CURRENT.reset(token)
                    if session and not session.closed:
                        session.state.response(model, resp)
                    yield resp
            except Exception:
                if session and not session.closed:
                    session.state.unknown_usage = True
                    session.state.step("当前模型请求失败，等待宿主重试或备用模型")
                raise
            finally:
                await stream.aclose()

        observed._feishu_card_observer = True
        self.original, self.wrapper = original, observed
        ToolLoopAgentRunner._iter_llm_responses = observed
        outer_original = ToolLoopAgentRunner._iter_llm_responses_with_fallback

        async def observed_outer(runner):
            stream = outer_original(runner)
            try:
                async for resp in stream:
                    event = getattr(getattr(runner.run_context, "context", None), "event", None)
                    session = event.get_extra(KEY) if event else None
                    if session and not session.closed and getattr(resp, "role", "") == "err":
                        session.failed = True
                        session.done_received = True
                        session.state.terminal = "本轮未完成"
                        session.state.step("可用模型未能完成请求，请稍后重试")
                    yield resp
            finally:
                event = getattr(getattr(runner.run_context, "context", None), "event", None)
                session = event.get_extra(KEY) if event else None
                if session and not session.closed and runner._is_stop_requested():
                    session.state.terminal = "已取消 / 已中断"
                await stream.aclose()

        self.outer_original, self.outer_wrapper = outer_original, observed_outer
        ToolLoopAgentRunner._iter_llm_responses_with_fallback = observed_outer
        from astrbot.core.tools.message_tools import SendMessageToUserTool
        self.send_original = SendMessageToUserTool.call
        original_send = self.send_original

        async def send_in_card(tool, context, **kwargs):
            event = getattr(getattr(context, "context", None), "event", None)
            session = event.get_extra(KEY) if event else None
            current = getattr(event, "unified_msg_origin", None)
            target = kwargs.get("session")
            messages = kwargs.get("messages")
            same_session = current and (not target or target == current)
            plain_only = (isinstance(messages, list) and bool(messages) and all(
                isinstance(item, dict) and str(item.get("type", "")).lower() == "plain"
                and isinstance(item.get("text"), str) and item["text"].strip()
                for item in messages))
            if session and not session.closed and same_session and plain_only:
                if session.stop_requested:
                    return "本轮已终止，未发送消息。"
                await session.accept_direct_text("\n\n".join(item["text"].strip() for item in messages))
                return "文本已合并至当前回复卡片，请勿重复发送；最终回答直接输出即可。"
            return await original_send(tool, context, **kwargs)

        self.send_wrapper = send_in_card
        SendMessageToUserTool.call = send_in_card
        self.handler = RetryHandler()
        logging.getLogger("astrbot").addHandler(self.handler)

    def uninstall(self):
        if self.send_original:
            from astrbot.core.tools.message_tools import SendMessageToUserTool
            if SendMessageToUserTool.call is self.send_wrapper:
                SendMessageToUserTool.call = self.send_original
        if self.original:
            from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
            if ToolLoopAgentRunner._iter_llm_responses is self.wrapper:
                ToolLoopAgentRunner._iter_llm_responses = self.original
        if self.outer_original:
            from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
            if ToolLoopAgentRunner._iter_llm_responses_with_fallback is self.outer_wrapper:
                ToolLoopAgentRunner._iter_llm_responses_with_fallback = self.outer_original
        if self.handler:
            logging.getLogger("astrbot").removeHandler(self.handler)


def tool_display_name(tool):
    """Optional presentation contract; routing continues to use the original tool name."""
    custom = getattr(tool, "display_name", None)
    return label(custom if isinstance(custom, str) and custom.strip() else tool.name, 200)


def tool_failure(result):
    """Honor the host's standard result status without interpreting tool-specific content."""
    return bool(getattr(result, "isError", False))
