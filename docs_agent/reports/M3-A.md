# M3-A 工作报告

- 任务：sirius-brain 的 VLM 客户端——大脑的"眼睛-思维"接口（QwenVLM + AgentConfig + local.md env 块装载 + fake transport 可测）
- 日期：2026-08-19
- 状态：完成（可选真实冒烟一条 PASS，见"验证方式"末尾）
- 验收：pytest 全绿 244/244（基线 193 + 新增 51，零破坏）；全部测试离线（fake transport / 127.0.0.1 回环），无真实联网调用进测试

## 交付物

| 文件 | 说明 |
|---|---|
| `sirius-brain/sirius_brain/agent/__init__.py` | Agent 包导出（QwenVLM / AgentConfig / VLMConfig / LoopConfig / VLMError / VLMAuthError / VLMResponse / ToolCall / VLMUsage / 消息构造函数 / parse_env_fenced_block 等） |
| `sirius-brain/sirius_brain/agent/config.py` | `VLMConfig`（base_url/api_key/model/enable_thinking/proxy/temperature/max_tokens/retries/timeout）+ `LoopConfig`（M3-B 预留）+ `AgentConfig`（聚合，复用既有 `BridgeConfig`）+ local.md ```env 围栏块解析 |
| `sirius-brain/sirius_brain/agent/vlm.py` | `QwenVLM` 客户端：请求组装 / 直连环境变量 / 重试 / 响应解析 / 用量累积；`urllib_transport` 默认 HTTP 层；消息构造 helper |
| `sirius-brain/tests/test_agent_vlm.py` | 51 项测试：配置装载 / 请求组装 / 图片消息 / 直连环境 / 响应解析 / 重试 / urllib transport 回环 |
| `sirius-brain/README.md` | 包结构树 + "Agent 包（M3-A）"一节 |

## 关键决策与理由

1. **同步接口而非 async**：默认 transport 是 urllib（阻塞）。做 async 就得引入 aiothttp/httpx（违反"零新第三方依赖"）或自己线程池。M3-B 的 asyncio 循环用 `asyncio.to_thread(vlm.chat, ...)` 包装即可——VLM 调用是秒级操作，线程切换开销可忽略。
2. **transport 接口 = `(request_dict) -> response_dict`**，request_dict 是自包含信封 `{url, headers, body, timeout, proxy}`，response_dict 是 `{status, body}`；网络层错误以异常表达。整个 HTTP 语义（URL 拼接、头、超时、代理）都在信封里可见，测试 fake 直接断言；真实 `urllib_transport` 只做"发出去+收回来"。
3. **直连三保险**：(a) 调用窗口内清空 HTTP(S)_PROXY/ALL_PROXY 环境变量并设 `NO_PROXY=*`（local.md 配方——urllib 在 Windows 会读注册表代理，`NO_PROXY=*` 使 `getproxies_environment()` 非空且全部 bypass，注册表配置连看都不看）；(b) 真实 transport 再显式 `ProxyHandler({})`（空 dict = 无视环境+注册表强制直连）；(c) 配置了 `proxy` 时反向：三个代理变量指向它、NO_PROXY 清空。环境变量在调用窗口结束**恢复原值**（不污染进程环境）。
4. **重试语义**：`retries=3` = 初试失败后最多再试 3 次（共 4 次请求），与 BridgeConfig.max_reconnects 的口径一致；可重试集 = 429 / 5xx / 网络错误（transport 抛的任何异常），指数退避 `retry_base_delay * 2^(n-1)` 封顶 30s；401/403 → `VLMAuthError`、其余 4xx → `VLMError`，一律不重试。退避基数走构造器参数 `retry_base_delay`（默认 1.0，测试传 0）而不进 VLMConfig——它是可测性旋钮，不是部署配置。
5. **enable_thinking 放请求体根级**（非 message 内、非 chat_template_kwargs）：local.md「识图/VLM」节实测配方，qwen3.7-plus DashScope 兼容模式验证过。
6. **temperature/max_tokens 默认 None = 不下发**（用服务端默认），显式配置才进请求体——避免无意覆盖 DashScope 默认采样。
7. **key 安全**：`from_local_md` 只读 gitignored 文件、`from_env` 只读环境变量；无任何构造路径接受字面 key 常量；测试全用 `sk-test` 假 key。
8. **不引入 pydantic 建模请求**：请求/响应走纯 dict——OpenAI 兼容面很大且 DashScope 有自有扩展（enable_thinking/reasoning_content），用 pydantic 严格建模反而脆；解析层对结构错误给带 code 的清晰 `VLMError`（对齐 bridge 客户端 -32000 段合成码惯例）。

## 实现要点 / API 笔记

### 请求体样例（`chat()` 组装，fake transport 抓到的真实信封）

```jsonc
// POST {base_url}/chat/completions，Headers: Authorization: Bearer <key>
{
  "model": "qwen3.7-plus",
  "enable_thinking": false,          // 根级（DashScope qwen3 系配方）
  "temperature": 0.7,                // 配了才出现（None=省略）
  "max_tokens": 1024,                // 配了才出现
  "messages": [
    {"role": "system", "content": "你是 Minecraft 陪玩…"},
    {"role": "user", "content": [     // 多模态：文本段 + 图片段（顺序保留）
      {"type": "text", "text": "背包里有什么？"},
      {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ…"}}
    ]},
    {"role": "assistant", "content": "", "tool_calls": [/* 历史轮原样回传 */]},
    {"role": "tool", "tool_call_id": "call_1", "content": "{\"slots\": […]}"}
  ],
  "tools": [{"type": "function", "function": {"name": "screenshot",
             "description": "…", "parameters": {/* JSON Schema */}}}],
  "tool_choice": "auto"               // 仅与 tools 同给时出现
}
```

tools 接受简写 `{"name","description","parameters"}`（自动包成 OpenAI 完整形态）或完整形态原样直通；`**extra_body` 并入根级（top_p 等）。

### 直连注意（Windows 特有坑，测试抓出来过）

**Windows 的 `os.environ` 键不区分大小写**（一律按大写归一，写 `http_proxy` 实际落在 `HTTP_PROXY`）。第一版把大小写各一套共 8 个变量都存/恢复，恢复时小写条目（None→pop）把大写条目刚恢复的代理值又删了。修正：`os.name == "nt"` 下只管大写一组（HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY），POSIX 下大小写两组都管。这是本任务唯一一个实现期 bug，靠"恢复后环境原样"断言暴露。

另：`_network_env` 是进程级 os.environ 的临时重塑，多个不同代理配置的 QwenVLM 并发调用会互抢——M3 单客户端 + 同步调用，安全；若未来多实例并发需改成锁或显式 ProxyHandler 优先。

### fake 注入模式（M3-B 及后续测试照抄）

```python
calls = []
def fake_transport(request):            # 与 urllib_transport 同签名
    calls.append(request)               # 断言请求信封（url/headers/body/proxy）
    return {"status": 200, "body": {...chat completion...}}
    # 或 {"status": 429, "body": {...}} 触发重试；raise OSError(...) 触发网络错误路

vlm = QwenVLM(VLMConfig(api_key="sk-test"), transport=fake_transport,
              retry_base_delay=0.0)     # 0 退避：重试用例不真睡
response = vlm.chat([user_message("看图", images=[jpeg_bytes])], tools=[...])
```

`urllib_transport` 本体另有 2 项测试对 127.0.0.1 回环 `http.server` 验证（POST 路径/头/UTF-8 往返/非 2xx 返回 status+body）——与 bridge 测试用本地 WebSocket 回环同口径，不算外网。

### 响应解析要点

- `tool_calls[].function.arguments` 是 **JSON 字符串**（OpenAI 惯例）→ 解析成 dict；已是 dict 直通；空串/缺失 → `{}`；非法 JSON → `VLMError(CODE_INVALID_RESPONSE)` 带 arguments 原文
- `content` 为 None（纯 tool_calls 轮）→ `""`；usage 缺省全 0
- 结构不对（缺 choices/message 类型错等）→ `VLMError(CODE_INVALID_RESPONSE, "响应不是合法的 chat completion：…", data=原始body)`，不抛裸 KeyError

### 用量统计

实例属性跨调用累积：`call_count` / `prompt_tokens` / `completion_tokens` / `total_tokens` / `last_elapsed`（秒），`reset_usage()` 清零；每次调用一条 INFO 日志（model/finish_reason/耗时/单次与累计 tokens）。

## 验证方式

- pytest：**244/244 绿**（基线 193 零破坏 + 新增 51，覆盖：env 围栏块解析（中文正文/注释行/多块取首/伪围栏不误匹配）、from_env、显式参数覆盖、配置校验、请求组装（enable_thinking 根级且不在 message、tools 归一化、None 省略、tool_choice 约束、extra_body 合并、assistant 历史带 tool_calls 直通）、图片（PNG/JPEG 魔数嗅探、data-url 前缀与 b64 可解回、data: 字符串直通）、直连（transport 被调时代理已清+NO_PROXY=\*、调用后恢复、配 proxy 反向）、解析（纯文本/tool_calls/usage 累计/三类非法响应）、重试（429 两次后成功 3 调、500 可重试、429 耗尽 4 调后抛、401/403/400 不重试、网络错误重试与耗尽、retries=0）、urllib transport 回环 2 项）
- 中文文件严格 UTF-8 解码自检通过（本机 GBK 代码页纪律）
- **真实冒烟（可选项，用了真接口，明确标注）**：用 local.md 的真实 key 对 DashScope qwen3.7-plus 发一条纯文本请求（`AgentConfig.from_local_md` 装载、直连模式、无工具）——真实走 `urllib_transport` 全路径（含本 shell 里挂着代理环境变量的情况下直连成功）。实际输出：

```
INFO sirius_brain.agent.vlm VLM 调用完成：model=qwen3.7-plus finish=stop 耗时=1.17s prompt=28 completion=23 total=51（累计 51 tokens / 1 次）
smoke: model=qwen3.7-plus finish=stop elapsed=1.17s
       prompt=28 completion=23 total=51
       content='我是天狼星，你的专属 Minecraft 陪玩 AI，随时准备与你并肩探索方块世界的无限可能。'
```

一句话回复 23 completion tokens / 1.17s，无思考阶段开销（enable_thinking=false 生效的表现；客户端不透传 reasoning_content 字段，此为间接判据）。key 只存在于 gitignored 的 local.md；冒烟脚本跑完即删，未入库。

## 交接须知（给 M3-B 的接口清单）

```python
from sirius_brain.agent import (
    AgentConfig,          # .vlm / .bridge / .loop
    VLMConfig, LoopConfig,
    QwenVLM, VLMError, VLMAuthError,          # VLMAuthError ⊂ VLMError
    VLMResponse, ToolCall, VLMUsage,
    system_message, user_message, tool_result_message,
    to_data_url, sniff_image_mime,
    CODE_NETWORK_ERROR, CODE_INVALID_RESPONSE,
)

# 配置（key 只从这里来，勿手拼）
config = AgentConfig.from_local_md("local.md")     # 首个 ```env 围栏块
config = AgentConfig.from_env()                    # SIRIUS_VLM_* / SIRIUS_BRIDGE_* 回退
config = AgentConfig.from_local_md(p, vlm=VLMConfig(...))  # 显式整体覆盖优先

# 客户端（同步；循环里 asyncio.to_thread(vlm.chat, ...) 包装）
vlm = QwenVLM(config.vlm)                          # transport=/retry_base_delay= 可注入（测试用）
response: VLMResponse = vlm.chat(
    messages,                                      # system/user/assistant/tool
    tools=[{"name","description","parameters"}],   # 简写或 OpenAI 完整形态
    tool_choice="auto",                            # 可省
)                                                  # 失败：VLMError(.code/.message/.data) / VLMAuthError / ValueError(本地拼装错)

# 消息构造
system_message("人格与规则")                        # → {"role":"system","content":...}
user_message("看到什么", images=[jpeg_bytes])       # → 多模态 user（b64 data-url 自动拼）
tool_result_message(call.id, {"ok": True})          # 工具结果回传（dict 自动 JSON 化）

# 响应消费
response.content / response.has_tool_calls / response.finish_reason / response.usage
for call in response.tool_calls:                   # call.id / call.name / call.arguments(dict)
    ...执行后 tool_result_message(call.id, result) → 下一轮 messages.append(assistant原消息)…
# 多轮回传：assistant 消息（含其 tool_calls）原样 append 进 messages 即可（_normalize 只校验 role）

# 用量（实例属性，循环全程可随时读）
vlm.call_count / vlm.prompt_tokens / vlm.completion_tokens / vlm.total_tokens / vlm.last_elapsed

# 预留：config.loop.max_steps=25 / min_interval=0.0（LoopConfig，M3-B 填语义）
```

- 已知限制：不支持 stream（M3 不需要）；单条消息图片数量不设限（DashScope 限制由服务端 400 兜底）；`_network_env` 进程级重塑要求"单客户端实例+同步调用"；历史轮 messages 不做 token 预算裁剪（M4+ 上下文管理的事）
- 关联报告：M1-D（bridge 客户端，AgentConfig.bridge 直接复用其 BridgeConfig）；M3-B（消费本接口搭循环）；local.md「识图/VLM」节（配方权威）
