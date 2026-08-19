"""agent 包 VLM 客户端与配置装载测试（M3-A）。零网络：

- ``QwenVLM`` 全部逻辑（请求组装/重试/直连环境变量/响应解析）走可注入 fake
  transport（callable(request_dict)->response_dict），不触网
- 唯一例外是 ``urllib_transport`` 本体：对 127.0.0.1 回环 HTTP 服务测真实
  urllib 路径（与 bridge 测试用本地 WebSocket 回环同口径，不算外网）
"""

import base64
import json
import os
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from sirius_brain.agent import (
    CODE_INVALID_RESPONSE,
    CODE_NETWORK_ERROR,
    AgentConfig,
    LoopConfig,
    QwenVLM,
    VLMConfig,
    VLMError,
    VLMAuthError,
    parse_env_fenced_block,
    sniff_image_mime,
    system_message,
    tool_result_message,
    user_message,
    urllib_transport,
)

# ---------------------------------------------------------------- 构造工具


def text_response(content: str = "好的", *, prompt: int = 10, completion: int = 5):
    """标准纯文本 chat completion。"""
    return {"status": 200, "body": {
        "model": "qwen3.7-plus",
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": content},
        }],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                  "total_tokens": prompt + completion},
    }}


def tool_call_response():
    """带两条 tool_calls：arguments 一条 JSON 字符串、一条已是 dict。"""
    return {"status": 200, "body": {
        "model": "qwen3.7-plus",
        "choices": [{
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "type": "function",
                     "function": {"name": "screenshot",
                                  "arguments": '{"tier":"gui"}'}},
                    {"id": "call_2", "type": "function",
                     "function": {"name": "getStats", "arguments": {}}},
                ],
            },
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }}


def http_error_response(status: int, message: str = "quota exceeded"):
    return {"status": status, "body": {"error": {"message": message, "code": "Err"}}}


def make_vlm(responses, *, config: VLMConfig | None = None,
             calls: list | None = None, **client_kwargs) -> QwenVLM:
    """造一个 fake transport 的 QwenVLM：按序吐 responses（异常项则抛出）。

    只剩最后一个响应时复用它（重试耗尽场景：恒定失败）。每个发出的请求信封
    记入 ``calls`` 供断言。
    """
    queue = list(responses)

    def fake_transport(request):
        if calls is not None:
            calls.append(request)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    cfg = config or VLMConfig(api_key="sk-test")
    return QwenVLM(cfg, transport=fake_transport, retry_base_delay=0.0, **client_kwargs)


LOCAL_MD = """# local —— 本机备忘（gitignored）

## 识图/VLM（中文正文与伪围栏不应干扰解析）

some ```python 示例（无 env 标签，不算）

```env
# 注释行要跳过
SIRIUS_VLM_BASE_URL=https://example.com/compatible-mode/v1
SIRIUS_VLM_API_KEY=sk-from-md
SIRIUS_VLM_MODEL=qwen-test
SIRIUS_VLM_ENABLE_THINKING=false
SIRIUS_VLM_PROXY=
```

第二个块取不到：

```env
SIRIUS_VLM_MODEL=second-block
```
"""


def write_local_md(tmp_path, content: str = LOCAL_MD):
    path = tmp_path / "local.md"
    path.write_text(content, encoding="utf-8")
    return path


# ================================================================ 配置装载


class TestConfigFromLocalMd:
    def test_parses_first_env_block(self, tmp_path):
        path = write_local_md(tmp_path)
        config = AgentConfig.from_local_md(path)
        assert config.vlm.base_url == "https://example.com/compatible-mode/v1"
        assert config.vlm.api_key == "sk-from-md"
        assert config.vlm.model == "qwen-test"  # 首块生效，第二块被忽略
        assert config.vlm.enable_thinking is False
        assert config.vlm.proxy is None  # SIRIUS_VLM_PROXY= 留空 = 直连

    def test_bridge_defaults_and_block_bridge_keys(self, tmp_path):
        # 块内无 bridge 键 → BridgeConfig 默认值
        config = AgentConfig.from_local_md(write_local_md(tmp_path))
        assert config.bridge.url == "ws://127.0.0.1:8765"
        assert config.loop.max_steps == 25  # LoopConfig 预留默认

        # 块内出现 SIRIUS_BRIDGE_* 则顺带装载
        md = LOCAL_MD.replace(
            "SIRIUS_VLM_PROXY=",
            "SIRIUS_VLM_PROXY=\nSIRIUS_BRIDGE_URL=ws://10.0.0.1:9000\n"
            "SIRIUS_BRIDGE_TOKEN=bridge-tok")
        config2 = AgentConfig.from_local_md(write_local_md(tmp_path, md))
        assert config2.bridge.url == "ws://10.0.0.1:9000"
        assert config2.bridge.token == "bridge-tok"

    def test_explicit_params_override_file(self, tmp_path):
        config = AgentConfig.from_local_md(
            write_local_md(tmp_path),
            vlm=VLMConfig(api_key="sk-explicit"),
            bridge=None,  # None = 照常装载
        )
        assert config.vlm.api_key == "sk-explicit"
        assert config.vlm.model == "qwen3.7-plus"  # 整体替代，不吃文件值

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            AgentConfig.from_local_md(tmp_path / "nope.md")

    def test_no_env_block_raises(self, tmp_path):
        path = tmp_path / "bare.md"
        path.write_text("# 只有中文正文，没有配置块", encoding="utf-8")
        with pytest.raises(ValueError, match="env 围栏块"):
            AgentConfig.from_local_md(path)

    def test_block_without_vlm_keys_raises(self, tmp_path):
        path = tmp_path / "other.md"
        path.write_text("```env\nSIRIUS_BRIDGE_URL=ws://x:1\n```\n", encoding="utf-8")
        with pytest.raises(ValueError, match="SIRIUS_VLM_"):
            AgentConfig.from_local_md(path)


class TestConfigFromEnv:
    def test_reads_sirius_vlm_env_vars(self, monkeypatch):
        monkeypatch.setenv("SIRIUS_VLM_BASE_URL", "https://env.example/v1")
        monkeypatch.setenv("SIRIUS_VLM_API_KEY", "sk-from-env")
        monkeypatch.setenv("SIRIUS_VLM_MODEL", "qwen-env")
        monkeypatch.setenv("SIRIUS_VLM_ENABLE_THINKING", "true")
        monkeypatch.setenv("SIRIUS_VLM_RETRIES", "5")
        monkeypatch.setenv("SIRIUS_VLM_TEMPERATURE", "0.2")
        monkeypatch.setenv("SIRIUS_BRIDGE_URL", "ws://127.0.0.1:9999")
        config = AgentConfig.from_env()
        assert config.vlm.api_key == "sk-from-env"
        assert config.vlm.enable_thinking is True
        assert config.vlm.retries == 5
        assert config.vlm.temperature == 0.2
        assert config.bridge.url == "ws://127.0.0.1:9999"

    def test_missing_vars_fall_back_to_defaults(self, monkeypatch):
        for name in list(os.environ):
            if name.startswith("SIRIUS_VLM_"):
                monkeypatch.delenv(name)
        config = AgentConfig.from_env()
        assert config.vlm.model == "qwen3.7-plus"
        assert config.vlm.enable_thinking is False


class TestParseEnvFencedBlock:
    def test_unit_parsing_rules(self):
        text = ("前置中文\n\n```env\n\n# 注释\nKEY_A = va lue\n"
                "KEY_B=x=y\n无效行\nKEY_C=42\n```\n后置")
        assert parse_env_fenced_block(text) == {
            "KEY_A": "va lue", "KEY_B": "x=y", "KEY_C": "42"}

    def test_no_block_returns_empty(self):
        assert parse_env_fenced_block("没有任何围栏") == {}


class TestVLMConfigValidation:
    def test_bad_base_url_rejected(self):
        with pytest.raises(ValueError, match="base_url"):
            VLMConfig(base_url="ftp://x")

    def test_empty_proxy_means_direct(self):
        assert VLMConfig(proxy="").proxy is None

    def test_numeric_ranges(self):
        with pytest.raises(ValueError, match="temperature"):
            VLMConfig(temperature=3.0)
        with pytest.raises(ValueError, match="max_tokens"):
            VLMConfig(max_tokens=0)
        with pytest.raises(ValueError, match="retries"):
            VLMConfig(retries=-1)
        with pytest.raises(ValueError, match="timeout"):
            VLMConfig(timeout=0)

    def test_bool_parsing_variants(self):
        assert VLMConfig.from_mapping(
            {"SIRIUS_VLM_ENABLE_THINKING": "1"}).enable_thinking is True
        assert VLMConfig.from_mapping(
            {"SIRIUS_VLM_ENABLE_THINKING": "NO"}).enable_thinking is False
        with pytest.raises(ValueError, match="SIRIUS_VLM_ENABLE_THINKING"):
            VLMConfig.from_mapping({"SIRIUS_VLM_ENABLE_THINKING": "大概"})

    def test_chat_completions_url(self):
        config = VLMConfig(base_url="https://x.example/v1/")
        assert config.chat_completions_url == "https://x.example/v1/chat/completions"

    def test_unknown_prefixed_keys_ignored(self):
        config = VLMConfig.from_mapping({"SIRIUS_VLM_FUTURE_KEY": "x"})
        assert config.model == "qwen3.7-plus"

    def test_loop_config_validation(self):
        with pytest.raises(ValueError, match="max_steps"):
            LoopConfig(max_steps=0)


# ================================================================ 请求组装


class TestRequestAssembly:
    def test_body_shape_and_headers(self):
        calls: list = []
        config = VLMConfig(api_key="sk-test", temperature=0.5, max_tokens=99,
                           enable_thinking=False)
        vlm = make_vlm([text_response()], config=config, calls=calls)
        vlm.chat(
            [system_message("你是陪玩"), user_message("看图")],
            tools=[{"name": "screenshot", "description": "截一张图",
                    "parameters": {"type": "object", "properties": {}}}],
            tool_choice="auto",
        )
        request = calls[0]
        assert request["url"].endswith("/chat/completions")
        assert request["headers"]["Authorization"] == "Bearer sk-test"
        assert request["headers"]["Content-Type"] == "application/json"
        assert request["timeout"] == config.timeout

        body = request["body"]
        assert body["model"] == "qwen3.7-plus"
        assert body["enable_thinking"] is False  # 根级参数（非 message 内）
        assert "enable_thinking" not in body["messages"][0]  # 且只出现在根级
        assert body["temperature"] == 0.5
        assert body["max_tokens"] == 99
        assert body["tool_choice"] == "auto"
        # 简写工具定义归一化为 OpenAI 完整形态
        assert body["tools"] == [{"type": "function", "function": {
            "name": "screenshot", "description": "截一张图",
            "parameters": {"type": "object", "properties": {}}}}]
        assert [m["role"] for m in body["messages"]] == ["system", "user"]
        assert body["messages"][1]["content"] == "看图"

    def test_optional_fields_omitted_when_none(self):
        calls: list = []
        vlm = make_vlm([text_response()], calls=calls)  # 默认 temperature/max_tokens=None
        vlm.chat([user_message("hi")])
        body = calls[0]["body"]
        assert "temperature" not in body
        assert "max_tokens" not in body
        assert "tools" not in body
        assert "tool_choice" not in body

    def test_full_form_tool_passthrough_and_enable_thinking_true(self):
        calls: list = []
        vlm = make_vlm([text_response()],
                       config=VLMConfig(api_key="k", enable_thinking=True), calls=calls)
        full_form = {"type": "function",
                     "function": {"name": "t", "description": "d",
                                  "parameters": {"type": "object"}}}
        vlm.chat([user_message("hi")], tools=[full_form])
        assert calls[0]["body"]["tools"] == [full_form]
        assert calls[0]["body"]["enable_thinking"] is True

    def test_extra_body_kwargs_merge_to_root(self):
        calls: list = []
        vlm = make_vlm([text_response()], calls=calls)
        vlm.chat([user_message("hi")], top_p=0.9)
        assert calls[0]["body"]["top_p"] == 0.9

    def test_tool_choice_without_tools_rejected(self):
        vlm = make_vlm([text_response()])
        with pytest.raises(ValueError, match="tool_choice"):
            vlm.chat([user_message("hi")], tool_choice="auto")

    def test_bad_message_role_rejected(self):
        vlm = make_vlm([text_response()])
        with pytest.raises(ValueError, match="role"):
            vlm.chat([{"role": "wizard", "content": "???"}])

    def test_empty_messages_rejected(self):
        vlm = make_vlm([text_response()])
        with pytest.raises(ValueError, match="messages"):
            vlm.chat([])

    def test_assistant_history_with_tool_calls_passthrough(self):
        """多轮：assistant 消息（含原生 tool_calls）与 tool 结果消息原样进请求体。"""
        calls: list = []
        vlm = make_vlm([text_response()], calls=calls)
        assistant = {"role": "assistant", "content": "",
                     "tool_calls": [{"id": "c1", "type": "function",
                                     "function": {"name": "f", "arguments": "{}"}}]}
        vlm.chat([user_message("hi"), assistant, tool_result_message("c1", {"ok": 1})])
        messages = calls[0]["body"]["messages"]
        assert messages[1] == assistant
        assert messages[2] == {"role": "tool", "tool_call_id": "c1",
                               "content": '{"ok": 1}'}


# ================================================================ 图片消息


class TestImageMessages:
    PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 8

    def test_sniff_image_mime(self):
        assert sniff_image_mime(self.PNG_BYTES) == "image/png"
        assert sniff_image_mime(self.JPEG_BYTES) == "image/jpeg"
        with pytest.raises(ValueError, match="JPEG/PNG"):
            sniff_image_mime(b"NOTIMAGE")

    def test_user_message_multi_image_data_urls(self):
        message = user_message("这是什么", images=[self.PNG_BYTES, self.JPEG_BYTES])
        parts = message["content"]
        assert message["role"] == "user"
        assert parts[0] == {"type": "text", "text": "这是什么"}
        assert parts[1]["type"] == "image_url"
        assert parts[1]["image_url"]["url"] == (
            "data:image/png;base64," + base64.b64encode(self.PNG_BYTES).decode("ascii"))
        assert parts[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        # b64 部分确实可解回原图
        b64 = parts[1]["image_url"]["url"].split(",", 1)[1]
        assert base64.b64decode(b64) == self.PNG_BYTES

    def test_text_only_user_message(self):
        assert user_message("纯文本") == {"role": "user", "content": "纯文本"}

    def test_images_without_text(self):
        parts = user_message(images=[self.JPEG_BYTES])["content"]
        assert [p["type"] for p in parts] == ["image_url"]

    def test_user_message_requires_something(self):
        with pytest.raises(ValueError):
            user_message()

    def test_data_url_string_passthrough(self):
        url = "data:image/gif;base64,R0lGOD"
        parts = user_message("g", images=[url])["content"]
        assert parts[1]["image_url"]["url"] == url

    def test_tool_result_message_serializes_dict(self):
        message = tool_result_message("call_9", {"slot": 3, "item": "oak_planks"})
        assert message["role"] == "tool"
        assert message["tool_call_id"] == "call_9"
        assert json.loads(message["content"]) == {"slot": 3, "item": "oak_planks"}
        assert tool_result_message("c", "纯文本")["content"] == "纯文本"


# ================================================================ 直连网络环境


class TestDirectNetworkEnv:
    def test_proxy_envs_cleared_before_transport_call(self, monkeypatch):
        """monkeypatch 设了代理后，transport 被调时环境必须已清（且 NO_PROXY=*）。"""
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy"):
            monkeypatch.setenv(name, "http://localhost:9674")
        monkeypatch.delenv("NO_PROXY", raising=False)
        seen = {}

        def spy_transport(request):
            seen["proxies"] = [os.environ.get(n) for n in
                               ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy")]
            seen["no_proxy"] = os.environ.get("NO_PROXY")
            seen["proxy_field"] = request["proxy"]
            return text_response()

        vlm = QwenVLM(VLMConfig(api_key="k"), transport=spy_transport,
                      retry_base_delay=0)
        vlm.chat([user_message("hi")])
        assert seen["proxies"] == [None, None, None, None]
        assert seen["no_proxy"] == "*"
        assert seen["proxy_field"] is None
        # 调用窗口结束恢复原值（不污染进程环境）
        assert os.environ["HTTP_PROXY"] == "http://localhost:9674"
        assert "NO_PROXY" not in os.environ

    def test_configured_proxy_is_applied(self, monkeypatch):
        monkeypatch.delenv("HTTP_PROXY", raising=False)
        monkeypatch.setenv("NO_PROXY", "internal.example")
        seen = {}

        def spy_transport(request):
            seen["http_proxy"] = os.environ.get("HTTP_PROXY")
            seen["no_proxy"] = os.environ.get("NO_PROXY")
            seen["proxy_field"] = request["proxy"]
            return text_response()

        vlm = QwenVLM(VLMConfig(api_key="k", proxy="http://localhost:9674"),
                      transport=spy_transport, retry_base_delay=0)
        vlm.chat([user_message("hi")])
        assert seen["http_proxy"] == "http://localhost:9674"
        assert seen["no_proxy"] is None
        assert seen["proxy_field"] == "http://localhost:9674"
        assert os.environ.get("HTTP_PROXY") is None  # 恢复
        assert os.environ["NO_PROXY"] == "internal.example"


# ================================================================ 响应解析


class TestResponseParsing:
    def test_plain_text(self):
        vlm = make_vlm([text_response("我看到一棵橡树")])
        response = vlm.chat([user_message("看到了什么")])
        assert response.content == "我看到一棵橡树"
        assert response.tool_calls == []
        assert not response.has_tool_calls
        assert response.finish_reason == "stop"
        assert (response.usage.prompt_tokens, response.usage.completion_tokens,
                response.usage.total_tokens) == (10, 5, 15)
        assert response.model == "qwen3.7-plus"

    def test_tool_calls_arguments_parsed_to_dict(self):
        vlm = make_vlm([tool_call_response()])
        response = vlm.chat([user_message("截图看看")])
        assert response.content == ""  # content None → 空串
        assert response.finish_reason == "tool_calls"
        assert response.has_tool_calls
        first, second = response.tool_calls
        assert (first.id, first.name) == ("call_1", "screenshot")
        assert first.arguments == {"tier": "gui"}  # JSON 字符串已解析成 dict
        assert second.arguments == {}  # dict 形态直通

    def test_usage_accumulates_across_calls(self):
        vlm = make_vlm([text_response(prompt=10, completion=5),
                        text_response(prompt=7, completion=3)])
        vlm.chat([user_message("一")])
        vlm.chat([user_message("二")])
        assert vlm.call_count == 2
        assert vlm.prompt_tokens == 17
        assert vlm.completion_tokens == 8
        assert vlm.total_tokens == 25
        assert vlm.last_elapsed >= 0.0
        vlm.reset_usage()
        assert (vlm.call_count, vlm.total_tokens) == (0, 0)

    def test_missing_choices_raises_invalid_response(self):
        vlm = make_vlm([{"status": 200, "body": {"model": "x"}}])
        with pytest.raises(VLMError) as excinfo:
            vlm.chat([user_message("hi")])
        assert excinfo.value.code == CODE_INVALID_RESPONSE
        assert "chat completion" in excinfo.value.message

    def test_empty_choices_list_raises(self):
        vlm = make_vlm([{"status": 200, "body": {"choices": []}}])
        with pytest.raises(VLMError) as excinfo:
            vlm.chat([user_message("hi")])
        assert excinfo.value.code == CODE_INVALID_RESPONSE

    def test_bad_tool_arguments_json_raises(self):
        body = {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "tool_calls": [
                {"id": "c", "function": {"name": "f", "arguments": "{broken"}}]}}]}
        vlm = make_vlm([{"status": 200, "body": body}])
        with pytest.raises(VLMError) as excinfo:
            vlm.chat([user_message("hi")])
        assert excinfo.value.code == CODE_INVALID_RESPONSE
        assert "arguments" in excinfo.value.message


# ================================================================ 重试


class TestRetry:
    def test_429_twice_then_success(self):
        calls: list = []
        vlm = make_vlm([http_error_response(429), http_error_response(429),
                        text_response("第三次成功")], calls=calls)
        response = vlm.chat([user_message("hi")])
        assert response.content == "第三次成功"
        assert len(calls) == 3

    def test_500_is_retryable(self):
        calls: list = []
        vlm = make_vlm([http_error_response(500), text_response()], calls=calls)
        assert vlm.chat([user_message("hi")]).content == "好的"
        assert len(calls) == 2

    def test_429_exhaustion_raises_after_retries_plus_one(self):
        calls: list = []
        vlm = make_vlm([http_error_response(429, "限流")], calls=calls)  # retries=3
        with pytest.raises(VLMError) as excinfo:
            vlm.chat([user_message("hi")])
        assert excinfo.value.code == 429
        assert "重试 3 次后仍失败" in excinfo.value.message
        assert "限流" in excinfo.value.message
        assert len(calls) == 4  # 初试 + 3 次重试

    def test_401_no_retry_auth_error(self):
        calls: list = []
        vlm = make_vlm([http_error_response(401, "InvalidApiKey")], calls=calls)
        with pytest.raises(VLMAuthError) as excinfo:
            vlm.chat([user_message("hi")])
        assert excinfo.value.code == 401
        assert "InvalidApiKey" in excinfo.value.message
        assert len(calls) == 1  # 不重试

    def test_403_is_auth_error(self):
        vlm = make_vlm([http_error_response(403)])
        with pytest.raises(VLMAuthError):
            vlm.chat([user_message("hi")])

    def test_400_no_retry(self):
        calls: list = []
        vlm = make_vlm([http_error_response(400, "bad request body")], calls=calls)
        with pytest.raises(VLMError) as excinfo:
            vlm.chat([user_message("hi")])
        assert excinfo.value.code == 400
        assert not isinstance(excinfo.value, VLMAuthError)
        assert "bad request body" in excinfo.value.message
        assert len(calls) == 1

    def test_network_error_retry_then_success(self):
        calls: list = []
        vlm = make_vlm([urllib.error.URLError("connection refused"),
                        OSError("reset"), text_response("恢复")], calls=calls)
        assert vlm.chat([user_message("hi")]).content == "恢复"
        assert len(calls) == 3

    def test_network_error_exhaustion(self):
        calls: list = []
        vlm = make_vlm([TimeoutError("timed out")], calls=calls)
        with pytest.raises(VLMError) as excinfo:
            vlm.chat([user_message("hi")])
        assert excinfo.value.code == CODE_NETWORK_ERROR
        assert len(calls) == 4

    def test_zero_retries_config(self):
        calls: list = []
        vlm = make_vlm([http_error_response(429)], calls=calls,
                       config=VLMConfig(api_key="k", retries=0))
        with pytest.raises(VLMError):
            vlm.chat([user_message("hi")])
        assert len(calls) == 1  # retries=0：只试一次


# ================================================================ urllib transport（回环）


class _CannedHandler(BaseHTTPRequestHandler):
    """127.0.0.1 回环 HTTP 服务：按序回预置响应，记录收到的请求。"""

    def do_POST(self):  # noqa: N802 —— http.server 约定
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append(
            {"path": self.path,
             "authorization": self.headers.get("Authorization"),
             "content_type": self.headers.get("Content-type"),
             "body": payload})
        status, body = self.server.responses.pop(0)
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # 静音（测试输出干净）
        return


@pytest.fixture()
def loopback_http():
    server = HTTPServer(("127.0.0.1", 0), _CannedHandler)
    server.requests = []
    server.responses = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class TestUrllibTransport:
    def test_roundtrip_request_and_response(self, loopback_http):
        loopback_http.responses.append(
            (200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}))
        request = {
            "url": f"http://127.0.0.1:{loopback_http.server_port}/v1/chat/completions",
            "headers": {"Content-Type": "application/json",
                        "Authorization": "Bearer sk-x"},
            "body": {"model": "qwen3.7-plus", "enable_thinking": False,
                     "messages": [{"role": "user", "content": "中文内容"}]},
            "timeout": 5.0,
            "proxy": None,
        }
        response = urllib_transport(request)
        assert response["status"] == 200
        assert response["body"]["choices"][0]["message"]["content"] == "ok"
        seen = loopback_http.requests[0]
        assert seen["path"] == "/v1/chat/completions"
        assert seen["authorization"] == "Bearer sk-x"
        assert seen["body"]["messages"][0]["content"] == "中文内容"  # UTF-8 往返无损

    def test_non_2xx_returns_status_and_body(self, loopback_http):
        loopback_http.responses.append((429, {"error": {"message": "busy"}}))
        request = {
            "url": f"http://127.0.0.1:{loopback_http.server_port}/x",
            "headers": {}, "body": {}, "timeout": 5.0, "proxy": None,
        }
        response = urllib_transport(request)
        assert response == {"status": 429, "body": {"error": {"message": "busy"}}}
