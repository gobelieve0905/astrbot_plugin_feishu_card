"""Platform-neutral rendering. Never interprets business questions or raw reasoning."""
from dataclasses import dataclass, field
import ipaddress
import json
import re
import time
from urllib.parse import urlsplit


def safe_text(value, limit=200):
    text = str(value or "")
    text = re.sub(r"(?:sk-|ou_|oc_|om_)[A-Za-z0-9_-]{8,}", "[已隐藏]", text)
    text = re.sub(r"(?i)(bearer\s+|(?:api[_-]?key|token|secret|password)\s*[:=]\s*)\S+", "[已隐藏]", text)
    return re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)[:limit]


def label(value, limit=200):
    return re.sub(r"[\[\]<>`*\\]", "", safe_text(value, limit)).replace("\n", " ")


def source_url(value):
    try:
        p = urlsplit(str(value))
        if p.scheme != "https" or not p.hostname or p.username or p.password or p.query or p.fragment:
            return ""
        if any(c in str(value) for c in "\n\r <>()"):
            return ""
        if p.hostname in {"localhost"} or "." not in p.hostname or p.hostname.endswith((".local", ".internal")):
            return ""
        try:
            if not ipaddress.ip_address(p.hostname).is_global:
                return ""
        except ValueError:
            pass
        if re.search(r"(?i)(token|secret|api[_-]?key|sk-)", p.path):
            return ""
        return str(value) if len(str(value)) <= 1000 else ""
    except ValueError:
        return ""


def pages(text, limit=10000):
    """Byte bounded, lossless text chunks; prefer paragraph/line boundaries."""
    result = []
    while text:
        raw = text.encode("utf-8")
        if len(raw) <= limit:
            result.append(text)
            break
        part = raw[:limit].decode("utf-8", errors="ignore")
        split = part.rfind("\n")
        if split > len(part) // 2:
            part = part[:split + 1]
        result.append(part)
        text = text[len(part):]
    return result or [""]


def panel(title, text, expanded=False):
    return {"tag": "collapsible_panel", "expanded": expanded,
            "background_color": "grey", "padding": "8px 12px 8px 12px",
            "border": {"color": "grey", "corner_radius": "6px"},
            "header": {"title": {"tag": "plain_text", "content": title},
                       "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined", "size": "16px 16px"},
                       "icon_position": "follow_text", "icon_expanded_angle": -180},
            "elements": [{"tag": "markdown", "content": text,
                          "text_size": "notation", "text_color": "grey"}]}


@dataclass
class State:
    question: str = ""
    start: float = field(default_factory=time.monotonic)
    status: str = "已收到，正在排队"
    text: str = ""
    terminal: str = ""
    ended: float = 0
    steps: list = field(default_factory=list)
    narratives: list = field(default_factory=list)
    tools: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    models: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    unknown_usage: bool = False
    calls: int = 0

    def step(self, text):
        text = label(text, 180)
        if not self.steps or self.steps[-1][1] != text:
            self.steps.append((int(time.monotonic() - self.start), text))
            self.steps = self.steps[-12:]
        self.status = text

    def source(self, title, url="", kind="资料"):
        item = (label(title, 120), source_url(url), label(kind, 30))
        if item[0] and item not in self.sources and len(self.sources) < 20:
            self.sources.append(item)

    def response(self, model, resp):
        if model not in self.models:
            self.models.append(model)
        if getattr(resp, "is_chunk", False):
            return
        usage = getattr(resp, "usage", None)
        if usage is None:
            self.unknown_usage = True
        else:
            try:
                values = [max(0, int(getattr(usage, key) or 0)) for key in ("input_other", "input_cached", "output")]
            except (ValueError, TypeError, AttributeError):
                self.unknown_usage = True
                return
            row = self.usage.setdefault(model, [0, 0, 0])
            for index, value in enumerate(values):
                row[index] += value


def render(state, config, part="", page=0, count=1, historical=False):
    def md(text, *, secondary=False):
        element = {"tag": "markdown", "content": text, "text_size": "notation" if secondary else "normal"}
        if secondary:
            element["text_color"] = "grey"
        return element
    elapsed = int((state.ended or time.monotonic()) - state.start)
    status = state.terminal or state.status
    elements = []
    if config.get("show_process", True) and (state.steps or state.narratives):
        process = "\n\n".join(state.narratives)
        timeline = "\n".join(f"{t}s · {s}" for t, s in state.steps)
        elements.append(panel("处理过程", "\n\n".join(x for x in (process, timeline) if x), config.get("expand_process", False)))
    elements.append({"tag": "hr"})
    # Use a model-authored leading heading, never infer business intent from keywords.
    title = label(state.question, 60) or "回复结果"
    heading = re.match(r"\A\s{0,3}#{1,6}[^\S\n]+([^\n]+)(?:\n|$)", state.text or part)
    if heading and ("\n" in heading.group(0) or state.terminal):
        title = label(heading.group(1).rstrip("# "), 100) or title
        if page == 0 and part.startswith(heading.group(0)):
            part = part[len(heading.group(0)):].lstrip("\n")
    title_element = md("**" + title + "**")
    title_element.update(text_size="heading", element_id="answer_title")
    elements.append(title_element)
    answer = md(part or ("本页内容已输出。" if historical else "已收到，正在处理你的请求…"))
    answer["element_id"] = "answer_body"
    answer["margin"] = "8px 0px 16px 0px"
    elements.append(answer)
    elements.append({"tag": "hr"})
    if config.get("show_tools", True) and state.tools:
        lines = []
        for tool in state.tools[-16:]:
            duration = int((tool.get("end") or time.monotonic()) - tool["start"])
            lines.append(f"**{label(tool['name'], 80)}** · {tool['status']} · {duration}s")
        elements.append(panel(f"工具调用（{len(state.tools)}）", "\n\n".join(lines)))
    if config.get("show_sources", True) and state.sources:
        lines = [f"- [{title}]({url}) · {kind}" if url else f"- {title} · {kind}" for title, url, kind in state.sources]
        elements.append(panel(f"知识与数据来源（{len(lines)}）", "\n".join(lines)))
    footer = []
    if config.get("show_usage", True):
        if state.models:
            footer.append(" → ".join(label(m, 90) for m in state.models[-4:]))
        if state.usage:
            inp = sum(v[0] + v[1] for v in state.usage.values())
            out = sum(v[2] for v in state.usage.values())
            footer.append(f"↑{inp:,} ↓{out:,}" + ("（已返回用量）" if state.unknown_usage else ""))
        elif state.terminal:
            footer.append("Token 用量未返回")
        rates = config.get("model_prices", {})
        if isinstance(rates, dict) and state.usage and all(m in rates for m in state.usage):
            try:
                cost = sum(sum(n * float(rates[m][k]) / 1000000 for n, k in zip(v, ("input", "cached_input", "output"))) for m, v in state.usage.items())
                if cost >= 0 and cost < 1e9:
                    footer.append(f"估算 {label(config.get('price_currency', 'USD'), 8)} {cost:.4f}" + ("（部分）" if state.unknown_usage else ""))
            except (ValueError, TypeError, KeyError):
                pass
    footer.extend([f"{elapsed}s", status])
    if count > 1:
        footer.append(f"第 {page + 1}/{count} 页")
    elements.append(md(" · ".join(footer), secondary=True))
    elements.append(md("你可以继续发送消息。", secondary=True))
    body = {"schema": "2.0", "config": {"wide_screen_mode": True, "update_multi": True,
            "summary": {"content": f"{status} · 飞书 Agent 卡片"}}, "body": {"elements": elements}}
    # Keep answer text lossless. Trim only optional display rows at whole-line boundaries.
    while len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > 26000:
        candidates = [e for e in elements if e.get("tag") == "collapsible_panel"]
        if not candidates:
            break
        largest = max(candidates, key=lambda e: len(e["elements"][0]["content"].encode("utf-8")))
        lines = largest["elements"][0]["content"].rstrip().split("\n")
        if len(lines) <= 1:
            elements.remove(largest)
        else:
            largest["elements"][0]["content"] = "\n".join(lines[:-1]).rstrip()
            title = largest["header"]["title"]
            if not title["content"].endswith(" · 部分记录"):
                title["content"] += " · 部分记录"
    return body
