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

    def install(self):
        from astrbot import __version__
        from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        if str(__version__).lstrip("v") != "4.28.0":
            raise RuntimeError("当前仅验收 AstrBot 4.28.0；保留原生回复")
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
        self.handler = RetryHandler()
        logging.getLogger("astrbot").addHandler(self.handler)

    def uninstall(self):
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


def tool_failure(tool, result):
    """Read only the declared API result envelope, never display its raw error body."""
    import json
    failed = bool(getattr(result, "isError", False))
    status = None
    if getattr(tool, "result_status_format", None) == "api_import_v1":
        content = getattr(result, "content", [])
        for block in content if isinstance(content, list) else []:
            text = getattr(block, "text", None)
            if not isinstance(text, str) or len(text) > 100000:
                continue
            try:
                value = json.loads(text)
            except (ValueError, RecursionError):
                continue
            if isinstance(value, dict) and value.get("ok") is False:
                failed = True
                code = value.get("status")
                if type(code) is int and 100 <= code <= 599:
                    status = code
    return failed, status
