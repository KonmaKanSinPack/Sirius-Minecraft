# sirius-brain

Sirius（天狼星）Minecraft AI 陪玩项目的 Python 后端大脑。

- 协议规格权威来源：`../sirius-technical.md` §8.2（Bridge Mod 协议）与 §5（任务卡/报告协议）
- 当前状态：M0 协议冻结 —— `sirius_brain/protocol/` 中的 pydantic v2 模型定义全部协议帧

## 包结构

```
sirius_brain/
  protocol/        # 协议帧 pydantic 模型（信封 / 工具参数 / NEKO 兼容帧 / 任务卡 / 报告）
  mock/            # mock bridge（假身体）：可脚本化 + 可回放的 WebSocket 服务
tests/             # 协议模型 / mock bridge 真实回环测试
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
