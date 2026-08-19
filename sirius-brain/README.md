# sirius-brain

Sirius（天狼星）Minecraft AI 陪玩项目的 Python 后端大脑。

- 协议规格权威来源：`../docs_agent/sirius-technical.md` §8.2（Bridge Mod 协议）与 §5（任务卡/报告协议）
- 当前状态：M0 协议冻结 —— `sirius_brain/protocol/` 中的 pydantic v2 模型定义全部协议帧

## 包结构

```
sirius_brain/
  protocol/        # 协议帧 pydantic 模型（信封 / 工具参数 / NEKO 兼容帧 / 任务卡 / 报告）
  mock/            # mock bridge（假身体）：可脚本化 + 可回放的 WebSocket 服务
  bridge/          # Bridge 客户端：大脑连接身体的统一入口（对 mock 与真 Mod 同一协议）
  agent/           # Agent 包：VLM 客户端（M3-A）+ 最小大脑循环 AgentLoop/工具注册表（M3-B）
schema/            # JSON Schema 冻结产物（Java 侧消费，export_schema.py 生成，需提交）
tests/             # 协议模型 / mock bridge / schema 导出 / bridge 客户端 / agent VLM / agent 循环测试
```

## 开发

使用 [uv](https://docs.astral.sh/uv/) 管理：

```sh
uv sync          # 安装依赖（创建 .venv）
uv run pytest    # 运行测试
```

## 与 sirius-bridge 的通信

WebSocket 上的 JSON 协议（MCP 语义）：

- 后端 → Mod：工具调用帧（请求-响应，JSON Schema 校验 + capabilities/list 版本协商）
- Mod → 后端：事件推送帧 `{type:"notification", event, data, timestamp, seq}`
- NEKO 兼容帧：`task` / `task_finished`（task_id 必须原样回传）

## 协议 Schema 导出（Java 侧消费入口）

协议的运行时权威定义是 `sirius_brain/protocol/` 的 pydantic 模型；同一定义可冻结为 JSON Schema 供 sirius-bridge（Java）直接消费——**Java 侧不读 Python 代码，只读 schema 产物**。

```sh
# 导出到默认目录 sirius-brain/schema/（改模型后必须重跑并提交）
.venv\Scripts\python.exe -m sirius_brain.protocol.export_schema

# 自定义输出目录
.venv\Scripts\python.exe -m sirius_brain.protocol.export_schema --output <DIR>
```

产物结构（**需提交进版本库**）：

```
schema/
  index.json              # 汇总索引：全部文件清单 + 协议版本 1.0 + 导出时间
  frames/<Frame>.json     # 信封帧（request/response/notification）+ NEKO 兼容帧（task/task_finished）
  tools/<method>.json     # 各工具 params 契约（文件名 = 方法名，含 '.'，如 input.click.json）
  tasks/<Model>.json      # 大脑内部任务卡 / 执行器报告
```

Java 侧消费建议：

- 每个文件都是**自包含**的独立文档：嵌套模型/枚举内联进该文件 `$defs`，`$ref` 一律为 `#/` 片段，无跨文件引用——单文件加载即可校验
- 方言为 **draft 2020-12**（每个文件首键 `$schema` 已显式声明）
  - networknt：`JsonSchemaFactory.getInstance(SpecVersion.VersionFlag.V202012)` 完整支持
  - everit-org/json-schema：未知方言 URI 被忽略，按 draft-7 语义校验（`prefixItems` 视为未知关键字，校验偏宽但不报错）
- 依赖驱动：`tests/test_schema_export.py` 会对比仓库内 `schema/` 与代码重导出结果，改了 pydantic 模型忘了重导出会直接红
- NEKO 兼容帧（`task`/`task_finished`）与自研 MCP 语义帧的完整映射见 [`../docs_agent/protocol-neko-mapping.md`](../docs_agent/protocol-neko-mapping.md)

## Mock Bridge 用法（假身体）

`sirius_brain/mock/` 是一个可脚本化、可回放的 WebSocket 服务，模拟真 Bridge Mod（M3 之前大脑全部逻辑对它开发）。帧校验完全复用 `protocol/` 的 pydantic 模型。

### 启动

```sh
# 默认剧本，监听 ws://127.0.0.1:8765
.venv\Scripts\python.exe -m sirius_brain.mock

# 自定义端口 + 行为脚本 + 自动回放录制帧（倍速加速）
.venv\Scripts\python.exe -m sirius_brain.mock --port 8765 --script scene.json --replay frames.jsonl --replay-speed 10

# 生成一份示例行为脚本（含各字段注释性示例）
.venv\Scripts\python.exe -m sirius_brain.mock --init-script scene.json
```

### 行为脚本（JSON）

mock 的行为由脚本驱动：收到某方法的工具调用回什么结果、延迟多少毫秒；收到 task 帧后延迟多久回哪种状态的 `task_finished`。

```jsonc
{
  "protocol_version": "1.0",              // capabilities/list 协商的协议版本
  "capabilities": [ /* 可选，默认从 protocol.TOOL_PARAMS 全量派生；可裁剪以模拟弱身体 */ ],
  "tools": {
    "getStats":   { "result": {"health": 20}, "delay_ms": 30 },
    "screenshot": { "result": {"tier": "full", "image_b64": "..."}, "delay_ms": 120 },
    "input.text": { "error": {"code": -32000, "message": "GUI 未打开"} }
  },
  "task_rules": [                          // 按序取第一条 match（task 文孙子串，null=兜底）命中
    { "match": "挖矿", "status": "failed", "text": "镐子断了", "delay_ms": 3000 },
    { "match": "合成", "status": "ok", "text": "合成完成", "delay_ms": 1500 },
    { "status": "ok", "delay_ms": 500 }    // catch-all
  ],
  "default_task": { "status": "ok", "delay_ms": 0 }   // task_rules 全不命中时（默认立即 ok）
}
```

行为要点：

- 工具在能力清单内但未编排 → 回通用成功 `{"ok": true, "method": ..., "echo": <params>}`；不在清单 → 错误 `-32601`；参数不过 JSON Schema → `-32602`
- 非法帧（非 JSON / 帧模型校验失败 / 未知 type）→ 回 `ToolCallResponse.error`（`-32700` / `-32600`），尽力回传请求 id
- `task_finished` 的 `task_id` 一律原样回传；多任务并发时按各自延迟乱序完成，靠 id 归属

### 事件推送与回放

```python
from sirius_brain.mock import MockBridgeServer, MockScript, load_replay

script = MockScript.from_json_file("scene.json")
async with MockBridgeServer(script, port=0) as server:   # port=0 随机端口，server.url 取实际地址
    await server.push_notification("fire", {"source": "creeper"}, EventLevel.CRITICAL)
    server.start_replay(load_replay("frames.jsonl"), speed=10)
```

- `push_notification` 广播到所有活跃连接，`seq` 每连接从 0 单调递增；事件分级注入 `data["level"]`（帧模型无 level 字段，规格语义放载荷里）
- 回放文件为 JSONL（每行一帧，支持 `#` 注释行）：`{"event": "chat", "data": {}, "level": "INFO", "delay_ms": 500}` 或整帧行 `{"type": "notification", "event": ..., "timestamp": ...}`；任一行带 `timestamp` 即按时间轴（首行归零）墙钟调度，否则按各行 `delay_ms` 顺序推；回放时 seq 重新编号，无连接时丢帧不中断

### 测试

`tests/test_mock_bridge.py` 跑真实 WebSocket 回环（随机端口，不 mock websockets 库）；项目未装 pytest-asyncio，异步场景用 `asyncio.run()` 驱动。

## Bridge 客户端（大脑连接身体的统一入口）

`sirius_brain/bridge/` 是大脑侧的 WebSocket 客户端 `BridgeClient`：**对两种身体同样工作**——mock（上文）与真 Bridge Mod（NeoForge，M1-B/C）协议一致，这是"大脑不绑死身体"的第一次实战。收发帧全部复用 `protocol/` 的 pydantic 模型，不重复定义协议类型。

### 用法

```python
from sirius_brain.bridge import BridgeClient, BridgeError

# 建议：事件 / task_finished handler 在 connect() 之前注册（身体可能一连上就推帧）
client = BridgeClient("ws://127.0.0.1:8765", token="s3cret")

@client.on_event("fire")          # 事件订阅 handler（"*" = 通配所有事件）
def on_fire(frame): ...           # frame: NotificationFrame（seq 单调性已校验）

@client.on_task_finished          # NEKO task_finished 回调（status 为五态枚举）
def on_finished(frame): ...

async with client:
    info = await client.capabilities()          # 能力协商：能力清单 + protocol_version
    stats = await client.call("getStats")       # 工具调用 RPC（id 自动配对）
    await client.subscribe_events(["fire"])     # events.subscribe 工具的便捷封装
    await client.command("/give @s diamond 1")  # 聊天/命令编排：input.key T → input.text → ENTER（M2-D）
    task_id = await client.send_task("挖一组铁矿")  # NEKO task 帧（fire-and-forget）
```

行为要点：

- **token 握手（best-effort）**：配置了 token 时连接后首条消息发送 `{"type":"hello","token":...,"protocol_version":"1.0"}`（spec §8.2 安全模型，真 Mod 要求）。mock 不校验也不回应 hello，客户端兼容"身体不回应"的情况——握手有超时上限、绝不阻塞后续调用，结果记在 `client.hello_result` / `await client.wait_hello()`
- **工具调用 RPC**：`call(method, params, timeout)` 正常返回 `result`；身体回错误帧（-32601/-32602 等）抛 `BridgeError(code, message, data)`；超时抛 `TimeoutError`；断线时在途请求立刻以 `BridgeError(CODE_CONNECTION_LOST)` 失败
- **断线自动重连**：次数（`max_reconnects`，None=无限）与指数退避（`reconnect_base_delay` 封顶 `reconnect_max_delay`）可配；状态变化经 `on_state_change(state, detail)` 回调（CONNECTING/CONNECTED/RECONNECTING/DISCONNECTED）。`connect()` 首连失败立即报错、不自动重试
- **事件分发**：后台接收循环按 event 名分发给已注册 handler；seq 乱序只告警不致命；收到无法识别的帧类型忽略并记录（前向兼容）

### 配置

`BridgeConfig`（url / token / 各超时 / 重连策略）支持三种来源：

```python
from sirius_brain.bridge import BridgeConfig

BridgeConfig.from_json_file("bridge.json")   # {"url": "ws://...", "token": "...", "request_timeout": 10}
BridgeConfig.from_env()                      # SIRIUS_BRIDGE_URL / _TOKEN / _REQUEST_TIMEOUT / _MAX_RECONNECTS …
```

### CLI 冒烟

```sh
# 连接 mock（或真 Mod），打印能力协商结果 + 订阅事件一行，然后退出
.venv\Scripts\python.exe -m sirius_brain.bridge --url ws://127.0.0.1:8765 [--token xxx]

# 连不上给清晰错误（退出码 1）；--wait N 可停留 N 秒打印期间收到的事件推送
```

### 测试

`tests/test_bridge_client.py` 对 mock 跑真实回环：能力协商往返、工具调用（result/error/timeout 三路）、token hello 与 mock 互通、task_finished 回调（特殊字符 task_id + 五态枚举全覆盖）、事件推送（seq 递增）、未知帧忽略 + seq 乱序容忍（对裸推送服务注入）、断线重连与在途请求失败、命令编排（T→text→ENTER 出站顺序 + 错误透传，M2-D）、配置装载（JSON/环境变量）。

## Agent 包（M3-A：VLM 客户端）

M3-A 交付大脑侧 VLM 客户端（`QwenVLM`，DashScope OpenAI 兼容 + 原生 tool-calling + 国内直连 + 重试）与配置装载（`AgentConfig.from_local_md()/from_env()`，key 只从 gitignored 的 local.md ```env 围栏块或环境变量来）。细节见 [`../docs_agent/reports/M3-A.md`](../docs_agent/reports/M3-A.md)；把 VLM 接成整机循环的是上文"Agent 循环（M3-B）"。

```python
from sirius_brain.agent import AgentConfig, QwenVLM, user_message

config = AgentConfig.from_local_md("../local.md")
vlm = QwenVLM(config.vlm)
response = vlm.chat(
    [user_message("背包里有什么？", images=[jpeg_bytes])],
    tools=[{"name": "inventory", "description": "查背包",
            "parameters": {"type": "object", "properties": {}}}],
)  # 同步方法：asyncio 侧用 asyncio.to_thread(vlm.chat, ...) 包装
response.tool_calls[0].arguments  # {"...": ...} 已解析成 dict
```

## Agent 循环（M3-B）

M3-B 把 QwenVLM 与 BridgeClient 接成**最小整机大脑**：玩家在游戏聊天打字 → bot 感知 → VLM 决策 → 工具执行 → 游戏内回话。细节见 [`../docs_agent/reports/M3-B.md`](../docs_agent/reports/M3-B.md)。

### 组成

- `agent/tools.py`——**工具注册表**：从冻结产物 `schema/tools/*.json` 读参数 schema，组装 OpenAI function-calling 工具表（M3 白名单最小集 11 个：getStats/getGuiState/world.query/screenshot/lookAt/input.mouseMove/input.click/input.key/input.text + 自定义 command/finish）；每工具一个 `async handler(client, args) -> ToolOutcome`，白名单外/参数错在客户端本地拒绝；`ToolRegistry.register(ToolSpec(...))` 可扩展（M5 分层留口）
- `agent/loop.py`——**AgentLoop**：chat 订阅指令入口（自回显双重过滤：抑制窗 + world.query 位置匹配自识别 uuid）、急停（"停下"/"stop" 在下一检查点断出并回话）、任务循环（系统提示 + 初始观测 → `asyncio.to_thread(vlm.chat, ...)` → 工具执行 → 结果回填，直至 finish / 纯文本回复 / max_steps / token 预算）、上下文管理（单条工具结果 >4000 字符截断、截图图像仅保留最近 1 张）
- `agent/__main__.py`——CLI 装配入口（见下）

```python
from sirius_brain.agent import AgentConfig, AgentLoop, QwenVLM, default_registry
from sirius_brain.bridge import BridgeClient

config = AgentConfig.from_local_md("../local.md")
client = BridgeClient(config.bridge)
vlm = QwenVLM(config.vlm)
loop = AgentLoop(client, vlm, config, persona="天狼星")
loop.install()                    # 注册 chat handler（connect 之前）
async with client:
    await loop.run()              # 常驻：订阅 chat → 串行执行任务队列
    # 单任务直驱（不经 chat）：run = await loop.run_task("丢一块石头给我")
```

### 任务语义

- **结束**：`finish(result)` 或纯文本回复 → `client.command(result)` 游戏内播报；max_steps（默认 25）/ token 预算（`LoopConfig.max_total_tokens`，默认 200000）用尽 → 播报"这个任务我先到这：（进度摘要）"；急停不播报（chat handler 已回话"好的，停下了"）
- **安全约束**写进系统提示（禁攻击/不丢重要物品/先观察再行动）；M3 白名单本身不含攻击类工具（look/input 原语之外无攻击入口）
- 单条工具结果文本超 4000 字符截断（保留头尾）；`screenshot` 的图像 bytes 附在下一轮 user 消息，历史消息里的旧图被裁掉（只留最近 1 张）

### CLI

```sh
# 连 mock（先起 python -m sirius_brain.mock）或真 Mod；打印就绪信息后常驻等待聊天指令
.venv\Scripts\python.exe -m sirius_brain.agent --local-md local.md \
    [--url ws://127.0.0.1:8765] [--token XXX] [--max-steps 25] [-v]
```

就绪信息：bridge 地址/hello 状态、自身 uuid（识别失败注明抑制窗兜底）、工具表、步数与 token 预算、VLM 模型。Ctrl+C 优雅退出。

### 测试

`tests/test_agent_loop.py`（19 项，全离线）：fake VLM（`ScriptedVLM` 剧本回放）× mock 双人剧本（`tests/fixtures/two_player_scene.json`，真实 WebSocket 回环）——全流程（指令→getStats→lookAt→command 话术→finish 播报上 wire）、自回显三连不触发新任务、急停断出与回话、max_steps/token 预算用尽播报、screenshot 图像附消息与旧图裁剪、白名单拒绝、截断边界、CLI 参数装载。
