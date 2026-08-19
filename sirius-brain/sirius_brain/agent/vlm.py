"""Qwen VLM 客户端：大脑的"眼睛-思维"接口。spec §10.1 M3-A。

DashScope OpenAI 兼容模式（``POST {base_url}/chat/completions``），qwen3.7-plus
配方来自 local.md「识图/VLM」节（实测：根级 ``enable_thinking:false`` + base64
data-url 图片 + 国内直连）。M3 三裁决：Bridge=哑管道，识图与决策全在 brain——
本模块就是 brain 侧唯一的出站 HTTP 依赖。

设计要点：
- **同步接口**：urllib 是阻塞的，``chat()`` 即同步方法；M3-B 的 asyncio 循环用
  ``asyncio.to_thread(vlm.chat, ...)`` 包装即可（不引入 aiohttp 等新依赖）
- **transport 可注入**：``transport: (request_dict) -> response_dict``，默认
  ``urllib_transport`` 真实实现。测试注入 fake 即可零网络覆盖全部逻辑
  （请求组装/重试/直连环境变量/解析）。request_dict 形如::

      {"url": ".../chat/completions", "headers": {...}, "body": {...请求体...},
       "timeout": 120.0, "proxy": None | "http://..."}

  response_dict 形如 ``{"status": 200, "body": {...chat completion...}}``；
  网络层错误（连不上/超时）由 transport 抛任意异常表达
- **国内直连**：每次调用的窗口内清空 HTTP(S)_PROXY/ALL_PROXY 并设 NO_PROXY=*
  （urllib 会读注册表代理，NO_PROXY=* 连注册表配置一并绕过），窗口结束恢复原值；
  真实 transport 再加显式空 ``ProxyHandler({})`` 双保险。配置了 ``proxy`` 则反其道
  （代理环境变量指向它、NO_PROXY 清空）
- **重试语义**：``retries`` = 初试失败后的最多重试次数（429/5xx/网络错误，
  指数退避 ``retry_base_delay * 2^(n-1)`` 封顶 30s）；401/403 抛 ``VLMAuthError``、
  其余 4xx 抛 ``VLMError``，一律不重试
- **用量统计**：实例属性累积（多次调用累计 prompt/completion/total tokens），
  每次调用打一条耗时 INFO 日志
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from .config import VLMConfig

logger = logging.getLogger(__name__)

# 客户端合成错误码（HTTP 错误直接带原始状态码 400/401/429/500…；合成码用
# -32000 段，与 bridge 客户端"实现方自定义"惯例一致）
CODE_NETWORK_ERROR = -32000
CODE_INVALID_RESPONSE = -32001

# transport 类型：请求信封 dict 进，{"status": int, "body": dict} 出
Transport = Callable[[dict[str, Any]], dict[str, Any]]

# 重试退避上限（秒）；基数由 QwenVLM(retry_base_delay=) 注入（测试传 0）
_RETRY_MAX_DELAY = 30.0

# 直连/代理模式要重塑的环境变量。**Windows 的 os.environ 键不区分大小写**
# （一律按大写归一，写 "http_proxy" 实际落在 "HTTP_PROXY" 上），故 nt 下只列
# 大写一组——若大小写各列一遍，恢复时小写项会把大写项刚恢复的值再删掉；
# POSIX 上大小写是不同变量，两组都管（urllib 两种拼法都会读）
if os.name == "nt":
    _PROXY_ENV_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
    _NO_PROXY_ENV_VARS = ("NO_PROXY",)
else:
    _PROXY_ENV_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                       "http_proxy", "https_proxy", "all_proxy")
    _NO_PROXY_ENV_VARS = ("NO_PROXY", "no_proxy")

# 允许的消息角色（OpenAI chat 语义；assistant 可带 tool_calls 原样回传）
_ALLOWED_ROLES = ("system", "user", "assistant", "tool")


class VLMError(Exception):
    """VLM 调用失败。code = HTTP 状态码（401/400/429/500…）或 -32000 段合成码。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class VLMAuthError(VLMError):
    """401/403：api_key 无效或无权限（不重试；检查 local.md 的 SIRIUS_VLM_API_KEY）。"""


# ---------------------------------------------------------------------- 结果类型


@dataclass(frozen=True)
class ToolCall:
    """一次原生 function-calling 调用（arguments 已解析成 dict）。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VLMUsage:
    """单次调用的 token 用量（服务端 usage 字段，缺省 0）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class VLMResponse:
    """chat() 的结构化返回。

    - ``content``：文本回复（无文本只有 tool_calls 时为 ``""``）
    - ``tool_calls``：模型发起的工具调用（原生 function-calling，已解析）
    - ``finish_reason``：``stop`` / ``tool_calls`` / ``length`` …（服务端语义）
    """

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: VLMUsage = field(default_factory=VLMUsage)
    model: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


# ---------------------------------------------------------------------- 消息构造


def sniff_image_mime(data: bytes) -> str:
    """按魔数识别图片 MIME（仅支持 JPEG/PNG，Bridge 截图两种格式）。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    raise ValueError("无法识别图片格式（仅支持 JPEG/PNG 魔数）")


def to_data_url(image: bytes | str) -> str:
    """图片 → base64 data-url。

    - ``bytes``：按魔数识别 MIME 再编码（``data:image/png;base64,…``）
    - ``str``：已是 ``data:`` URL 原样通过；裸 base64 串按 JPEG 包装（截图惯例）
    """
    if isinstance(image, str):
        if image.startswith("data:"):
            return image
        return "data:image/jpeg;base64," + image
    return f"data:{sniff_image_mime(image)};base64," + base64.b64encode(image).decode("ascii")


def system_message(text: str) -> dict[str, Any]:
    """system 消息。"""
    return {"role": "system", "content": text}


def user_message(text: str | None = None,
                 images: Sequence[bytes | str] = ()) -> dict[str, Any]:
    """user 消息：纯文本（content 为字符串）或 文本+多图（content 为分段列表）。

    图片顺序保留（text 段在前），每图一个 ``image_url`` 段——OpenAI 多模态格式。
    """
    image_urls = [to_data_url(image) for image in images]
    if not image_urls:
        if text is None:
            raise ValueError("user_message 需要 text 或 images 至少其一")
        return {"role": "user", "content": text}
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    parts.extend({"type": "image_url", "image_url": {"url": url}}
                 for url in image_urls)
    return {"role": "user", "content": parts}


def tool_result_message(tool_call_id: str, content: Any) -> dict[str, Any]:
    """tool 消息：把工具执行结果回传给模型（dict 自动序列化为 JSON 字符串）。"""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


# ---------------------------------------------------------------------- 客户端


class QwenVLM:
    """Qwen VLM 客户端（OpenAI 兼容 + 原生 tool-calling）。

    用法::

        from sirius_brain.agent import AgentConfig, QwenVLM, user_message

        config = AgentConfig.from_local_md("../local.md")
        vlm = QwenVLM(config.vlm)
        response = vlm.chat(
            [user_message("背包里有什么？", images=[jpeg_bytes])],
            tools=[{"name": "getStats", "description": "查状态",
                    "parameters": {"type": "object", "properties": {}}}],
        )
        if response.has_tool_calls:
            call = response.tool_calls[0]   # call.arguments 已是 dict

    用量统计（实例属性，跨调用累积）：``call_count`` / ``prompt_tokens`` /
    ``completion_tokens`` / ``total_tokens`` / ``last_elapsed``（秒）。
    """

    def __init__(self, config: VLMConfig | None = None, *,
                 transport: Transport | None = None,
                 retry_base_delay: float = 1.0) -> None:
        self.config = config if config is not None else VLMConfig()
        self._transport: Transport = transport if transport is not None else urllib_transport
        self.retry_base_delay = retry_base_delay
        # 用量统计累积器
        self.call_count = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.last_elapsed = 0.0

    # ------------------------------------------------------------------ 主入口

    def chat(self,
             messages: Sequence[Mapping[str, Any]],
             tools: Sequence[Mapping[str, Any]] | None = None,
             tool_choice: str | Mapping[str, Any] | None = None,
             **extra_body: Any) -> VLMResponse:
        """一次 chat/completions 调用（同步；asyncio 侧用 to_thread 包装）。

        - ``messages``：system/user/assistant/tool 消息（本模块的构造函数产出，
          或历史 assistant 消息原样回传——含其 tool_calls）
        - ``tools``：工具清单，接受简写 ``{"name","description","parameters"}``
          或 OpenAI 完整形态 ``{"type":"function","function":{…}}``
        - ``tool_choice``：仅与 ``tools`` 同给（"auto"/"none"/{"type":"function",…}）
        - ``**extra_body``：并入请求体根级的额外参数（top_p 等）
        - 返回 :class:`VLMResponse`；失败抛 ``VLMError`` / ``VLMAuthError``
        """
        request = self._build_request(messages, tools, tool_choice, extra_body)
        started = time.perf_counter()
        response = self._send_with_retry(request)
        result = _parse_chat_response(response.get("body"))
        elapsed = time.perf_counter() - started
        self.last_elapsed = elapsed
        self.call_count += 1
        self.prompt_tokens += result.usage.prompt_tokens
        self.completion_tokens += result.usage.completion_tokens
        self.total_tokens += result.usage.total_tokens
        logger.info("VLM 调用完成：model=%s finish=%s 耗时=%.2fs prompt=%d "
                    "completion=%d total=%d（累计 %d tokens / %d 次）",
                    result.model, result.finish_reason, elapsed,
                    result.usage.prompt_tokens, result.usage.completion_tokens,
                    result.usage.total_tokens, self.total_tokens, self.call_count)
        return result

    def reset_usage(self) -> None:
        """清零用量统计累积器。"""
        self.call_count = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.last_elapsed = 0.0

    # ------------------------------------------------------------------ 请求组装

    def _build_request(self,
                       messages: Sequence[Mapping[str, Any]],
                       tools: Sequence[Mapping[str, Any]] | None,
                       tool_choice: str | Mapping[str, Any] | None,
                       extra_body: dict[str, Any]) -> dict[str, Any]:
        if not messages:
            raise ValueError("messages 不能为空")
        if tool_choice is not None and not tools:
            raise ValueError("tool_choice 必须与 tools 一起给出")
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": [_normalize_message(message) for message in messages],
            # DashScope qwen3 系：根级开关思考（local.md 实测配方）
            "enable_thinking": self.config.enable_thinking,
        }
        if self.config.temperature is not None:
            body["temperature"] = self.config.temperature
        if self.config.max_tokens is not None:
            body["max_tokens"] = self.config.max_tokens
        if tools:
            body["tools"] = [_normalize_tool(tool) for tool in tools]
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
        body.update(extra_body)
        return {
            "url": self.config.chat_completions_url,
            "headers": {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            },
            "body": body,
            "timeout": self.config.timeout,
            "proxy": self.config.proxy,
        }

    # ------------------------------------------------------------------ 重试

    def _send_with_retry(self, request: dict[str, Any]) -> dict[str, Any]:
        """发请求 + 重试策略（429/5xx/网络错误指数退避；401/400 直接抛）。"""
        retries = self.config.retries
        attempt = 0
        while True:
            attempt += 1
            try:
                with self._network_env():
                    response = self._transport(dict(request))
            except VLMError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 网络层任何异常都按可重试处理
                if attempt > retries:
                    raise VLMError(
                        CODE_NETWORK_ERROR,
                        f"网络错误，重试 {retries} 次后仍失败：{type(exc).__name__}: {exc}",
                    ) from exc
                self._sleep_and_log(attempt, f"网络错误 {type(exc).__name__}: {exc}")
                continue
            status = int(response.get("status") or 0)
            if 200 <= status < 300:
                return response
            message = _extract_error_message(response.get("body"))
            if status in (401, 403):
                raise VLMAuthError(
                    status, f"认证失败（检查 local.md 的 SIRIUS_VLM_API_KEY）：{message}")
            if status == 429 or status >= 500:
                if attempt > retries:
                    raise VLMError(
                        status, f"HTTP {status}，重试 {retries} 次后仍失败：{message}",
                        data=response.get("body"))
                self._sleep_and_log(attempt, f"HTTP {status}：{message}")
                continue
            # 其余 4xx：请求本身有错，重试无意义
            raise VLMError(status, message or f"HTTP {status}", data=response.get("body"))

    def _sleep_and_log(self, attempt: int, reason: str) -> None:
        delay = min(self.retry_base_delay * 2 ** (attempt - 1), _RETRY_MAX_DELAY)
        logger.warning("VLM 第 %d 次尝试失败（%s），%.1fs 后重试", attempt, reason, delay)
        time.sleep(delay)

    # ------------------------------------------------------------------ 直连环境

    @contextmanager
    def _network_env(self):
        """transport 调用窗口内的代理环境变量重塑（离开窗口恢复原值）。

        - 直连（proxy=None）：清空全部 *_PROXY 并设 NO_PROXY=*——urllib 在 Windows
          会读注册表代理，NO_PROXY=* 让 getproxies_environment 非空且全部 bypass
        - 配置了 proxy：三个代理变量指向它、NO_PROXY 清空（环境变量优先于注册表）

        注意：os.environ 是进程级的，多个不同代理配置的 QwenVLM 并发调用会互抢；
        M3 只有一个客户端实例 + 同步调用，安全。
        """
        proxy = self.config.proxy
        saved: list[tuple[str, str | None]] = []

        def _set(name: str, value: str | None) -> None:
            saved.append((name, os.environ.get(name)))
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

        try:
            for name in _PROXY_ENV_VARS:
                _set(name, proxy)
            for name in _NO_PROXY_ENV_VARS:
                _set(name, None if proxy else "*")
            yield
        finally:
            for name, old in saved:
                if old is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old


# ---------------------------------------------------------------------- 解析


def _normalize_message(message: Mapping[str, Any]) -> dict[str, Any]:
    """消息浅校验 + 转 dict（角色白名单外的直接 ValueError，尽早暴露拼装错误）。"""
    if not isinstance(message, Mapping) or message.get("role") not in _ALLOWED_ROLES:
        raise ValueError(f"消息必须是含 role ∈ {_ALLOWED_ROLES} 的 dict，got {message!r}")
    return dict(message)


def _normalize_tool(tool: Mapping[str, Any]) -> dict[str, Any]:
    """工具定义归一化为 OpenAI 完整形态（简写 {"name",…} → {"type":"function",…}）。"""
    if isinstance(tool, Mapping) and tool.get("type") == "function":
        return dict(tool)
    if isinstance(tool, Mapping) and "name" in tool:
        return {"type": "function",
                "function": {k: v for k, v in tool.items() if k != "type"}}
    raise ValueError(f"tool 定义须含 name（或已是 type=function 完整形态），got {tool!r}")


def _parse_tool_call(raw: Any) -> ToolCall:
    """单条 tool_call 解析：arguments 是 JSON 字符串时解析成 dict（已是 dict 则直通）。"""
    try:
        call_id = raw["id"]
        function = raw["function"]
        name = function["name"]
        arguments = function.get("arguments")
    except (KeyError, TypeError) as exc:
        raise VLMError(CODE_INVALID_RESPONSE,
                       f"tool_call 结构不合法（缺 {exc}）", data=raw) from exc
    if arguments is None or arguments == "":
        parsed: dict[str, Any] = {}
    elif isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise VLMError(CODE_INVALID_RESPONSE,
                           f"tool_call {name!r} 的 arguments 不是合法 JSON：{arguments!r}",
                           data=raw) from exc
        if not isinstance(parsed, dict):
            raise VLMError(CODE_INVALID_RESPONSE,
                           f"tool_call {name!r} 的 arguments 必须是 JSON 对象，"
                           f"got {type(parsed).__name__}", data=raw)
    elif isinstance(arguments, dict):
        parsed = arguments
    else:
        raise VLMError(CODE_INVALID_RESPONSE,
                       f"tool_call {name!r} 的 arguments 类型不支持："
                       f"{type(arguments).__name__}", data=raw)
    return ToolCall(id=call_id, name=name, arguments=parsed)


def _parse_chat_response(body: Any) -> VLMResponse:
    """chat completion 响应 → VLMResponse（结构不对给清晰错误，不抛 KeyError）。"""
    try:
        choice = body["choices"][0]
        if not isinstance(choice, Mapping):
            raise TypeError(f"choices[0] 不是对象：{type(choice).__name__}")
        message = choice.get("message") or {}
        if not isinstance(message, Mapping):
            raise TypeError(f"message 不是对象：{type(message).__name__}")
        content = message.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise TypeError(f"content 须为字符串：{type(content).__name__}")
        tool_calls = [_parse_tool_call(raw) for raw in (message.get("tool_calls") or [])]
        usage_raw = body.get("usage") or {}
        usage = VLMUsage(
            prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
            completion_tokens=int(usage_raw.get("completion_tokens") or 0),
            total_tokens=int(usage_raw.get("total_tokens") or 0),
        )
        return VLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=str(choice.get("finish_reason") or "stop"),
            usage=usage,
            model=str(body.get("model") or ""),
        )
    except VLMError:
        raise
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise VLMError(CODE_INVALID_RESPONSE,
                       f"响应不是合法的 chat completion：{exc}", data=body) from exc


def _extract_error_message(body: Any) -> str:
    """从错误响应体提取人话（DashScope 兼容模式：{"error": {"message": …}}）。"""
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping) and error.get("message"):
            return str(error["message"])
        if body.get("message"):
            return str(body["message"])
        try:
            return json.dumps(body, ensure_ascii=False)[:200]
        except (TypeError, ValueError):
            return repr(body)[:200]
    return repr(body)[:200]


# ---------------------------------------------------------------------- 真实 transport


def urllib_transport(request: dict[str, Any]) -> dict[str, Any]:
    """默认 transport：urllib 直连实现（零新第三方依赖）。

    - 非 2xx 不抛：读出错误体返回 ``{"status": code, "body": …}``（重试层决策）
    - 代理确定性：``proxy`` 有值 → 显式 ProxyHandler 指向它；None → 显式空
      ProxyHandler（无视环境变量与 Windows 注册表代理，直连双保险）
    - 连不上/超时等网络层错误以异常形式抛出（重试层按网络错误处理）
    """
    payload = json.dumps(request["body"], ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(request["url"], data=payload, method="POST")
    for name, value in (request.get("headers") or {}).items():
        req.add_header(name, value)
    proxy = request.get("proxy")
    handler = urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {})
    opener = urllib.request.build_opener(handler)
    try:
        with opener.open(req, timeout=request.get("timeout", 60.0)) as response:
            return {"status": response.status, "body": _read_json(response)}
    except urllib.error.HTTPError as exc:
        try:
            error_body: Any = _read_json(exc)
        except Exception:  # noqa: BLE001 —— 错误体读不出来不掩盖原始状态码
            error_body = {}
        return {"status": exc.code, "body": error_body}


def _read_json(http_message: Any) -> Any:
    return json.loads(http_message.read().decode("utf-8"))
