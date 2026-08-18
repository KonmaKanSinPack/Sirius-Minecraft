# sirius-brain

Sirius（天狼星）Minecraft AI 陪玩项目的 Python 后端大脑。

- 协议规格权威来源：`../sirius-technical.md` §8.2（Bridge Mod 协议）与 §5（任务卡/报告协议）
- 当前状态：M0 协议冻结 —— `sirius_brain/protocol/` 中的 pydantic v2 模型定义全部协议帧

## 包结构

```
sirius_brain/
  protocol/        # 协议帧 pydantic 模型（信封 / 工具参数 / NEKO 兼容帧 / 任务卡 / 报告）
tests/             # 协议模型往返 / 枚举 / 校验测试
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
