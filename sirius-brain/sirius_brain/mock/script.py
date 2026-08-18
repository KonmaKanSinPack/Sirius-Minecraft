"""mock bridge 行为脚本模型：收到某类帧后回什么、延迟多少毫秒。

复用 T1 协议模型（不重复定义协议类型）；脚本本身是 mock 层的"剧本"，
可由 Python dict 构造，也可从 JSON 文件加载（便于写测试场景）。spec §10.1 M0「可脚本响应」。
"""

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from sirius_brain.protocol import Capability, TaskFinishedStatus, ToolCallError, TOOL_PARAMS


class ScriptedToolResponse(BaseModel):
    """收到 method 的工具调用时的剧本：延迟 delay_ms 后回 result 或 error。spec §8.2 请求-响应。"""

    result: Any = None
    error: ToolCallError | None = None
    delay_ms: float = Field(default=0, ge=0)


class ScriptedTask(BaseModel):
    """task 帧剧本：延迟 delay_ms 后回 task_finished（status 五态）。

    match 为 task 文本的子串匹配条件（None = 匹配一切，作 catch-all）。
    task_id 不参与匹配——无论匹配到哪条规则，回帧都原样回传 task_id。spec §8.2。
    """

    match: str | None = None
    status: TaskFinishedStatus = TaskFinishedStatus.OK
    text: str = ""
    delay_ms: float = Field(default=0, ge=0)


def default_capabilities() -> list[Capability]:
    """从 T1 TOOL_PARAMS 注册表派生能力清单：name=方法名，input_schema=参数 JSON Schema。"""

    return [
        Capability(name=method, version="1.0", input_schema=params.model_json_schema())
        for method, params in TOOL_PARAMS.items()
    ]


class MockScript(BaseModel):
    """mock bridge 的整体行为脚本。

    字段：
    - protocol_version：capabilities/list 响应里协商的协议版本
    - capabilities：能力清单（默认从 TOOL_PARAMS 派生，可覆盖以模拟裁剪能力的身体）
    - tools：method → 剧本；未编排的方法（但在能力清单内）回通用成功
    - task_rules：按序匹配的第一条生效（match 子串或 None）
    - default_task：task_rules 全不命中时的兜底剧本（默认立即 ok）
    """

    protocol_version: str = "1.0"
    capabilities: list[Capability] = Field(default_factory=default_capabilities)
    tools: dict[str, ScriptedToolResponse] = Field(default_factory=dict)
    task_rules: list[ScriptedTask] = Field(default_factory=list)
    default_task: ScriptedTask = Field(default_factory=ScriptedTask)

    @classmethod
    def from_json_file(cls, path: str | Path) -> "MockScript":
        """从 JSON 文件加载脚本（UTF-8）。"""
        with open(path, encoding="utf-8") as f:
            return cls.model_validate(json.load(f))

    def capability_names(self) -> set[str]:
        return {cap.name for cap in self.capabilities}

    def tool_response(self, method: str) -> ScriptedToolResponse | None:
        """取工具剧本；未编排返回 None（调用方回通用成功）。"""
        return self.tools.get(method)

    def task_outcome(self, task_text: str) -> ScriptedTask:
        """task 帧命中的剧本：task_rules 按序取第一条 match 为 None 或子串命中者。"""
        for rule in self.task_rules:
            if rule.match is None or rule.match in task_text:
                return rule
        return self.default_task
