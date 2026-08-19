"""M3-B 工具注册表：把 bridge 工具 + 大脑侧自定义工具组装成 VLM function-calling 工具表。

设计要点：
- **参数 schema 从冻结产物读**：``schema/tools/*.json`` 是 ``protocol.TOOL_PARAMS`` 的
  导出物（Java 侧同源），读它保证 VLM 看到的参数契约与 bridge 校验完全一致；
  客户端侧再用同一 ``TOOL_PARAMS`` pydantic 模型预校验——白名单外的/参数错的
  调用在本地就拒绝，不浪费一次 bridge 往返
- **M3 白名单最小集**：观察（getStats/getGuiState/world.query/screenshot）+ 视角
  （lookAt）+ 输入四原语 + command（说话/游戏指令，走 BridgeClient.command 编排）
  + finish（自定义控制工具：结束任务并在游戏内播报 result）
- **handler 统一签名**：``async handler(client, args) -> ToolOutcome``。观测结果压成
  紧凑 JSON 文本回填 VLM；screenshot 特殊——文本只回 ``[图像已附]``，JPEG bytes 放
  ``ToolOutcome.image``，由循环附进下一轮 user 消息
- **可扩展**：``ToolRegistry.register(ToolSpec(...))`` 随时挂新工具（M5 分层留口），
  白名单外的名字在 ``execute`` 一律 ``UnknownToolError``（即便 schema 目录里有，
  如 look / events.subscribe 不在 M3 白名单内）
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from sirius_brain.bridge.client import BridgeClient
from sirius_brain.protocol import TOOL_PARAMS

logger = logging.getLogger(__name__)

# 冻结 schema 产物目录：sirius-brain/schema/tools/（本文件在 sirius_brain/agent/ 下）
SCHEMA_TOOLS_DIR = Path(__file__).resolve().parents[2] / "schema" / "tools"

# M3 白名单：bridge 既有工具（schema 产物里读参数）+ 大脑侧自定义工具（command/finish）
BRIDGE_WHITELIST: tuple[str, ...] = (
    "getStats",
    "getGuiState",
    "world.query",
    "screenshot",
    "lookAt",
    "input.mouseMove",
    "input.click",
    "input.key",
    "input.text",
)
# 自定义工具名（不在 bridge schema 里，参数与 handler 都在本文件定义）
COMMAND_TOOL = "command"
FINISH_TOOL = "finish"

# 给 VLM 的工具用途说明（schema 的 description 是"getStats()。spec §8.2。"这类
# 内部记号，对模型无用；这里换成面向模型的操作指引）
TOOL_HINTS: dict[str, str] = {
    "getStats": "查看自身状态：生命/饥饿/氧气/经验/坐标(x,y,z)/维度/游戏模式/状态效果",
    "getGuiState": "查看当前打开的界面（背包/箱子/聊天框等）：widget 树与容器槽位"
                   "（含物品注册名与数量）。注意：槽位坐标是 gui-scaled，"
                   "input.mouseMove 收窗口像素，需按比例换算后使用",
    "world.query": "查询自己附近的实体（type=entities，含 uuid/名字/类型/坐标/生命）"
                   "或非空气方块（type=blocks）；range 为半径（格）",
    "screenshot": "截取当前游戏画面；图像会附加在下一条消息里供你查看",
    "lookAt": "把视线转到世界坐标 (x,y,z)（绝对转视角，用于看向目标/瞄准）",
    "input.mouseMove": "移动鼠标光标到窗口像素坐标 (x,y)",
    "input.click": "鼠标点击（button：0=左键 1=右键 2=中键；count=次数，默认 1）",
    "input.key": "按一个键（code 为 GLFW 键码：E=69 T=84 ENTER=257 W=87 A=65 S=83 D=68；"
                 "duration_ms 为按住时长；modifiers 如 [\"shift\"]）",
    "input.text": "向当前聚焦的文本框输入字符串（先开聊天框/输入框再输入）",
    "command": "在游戏聊天框发送一条文本：以 / 开头即游戏命令（如 /give），"
               "否则是普通聊天发言",
    "finish": "任务完成时调用：result 是要在游戏聊天里播报的结束语。"
              "调用后本任务结束，不再执行任何工具",
}


class UnknownToolError(Exception):
    """工具名不在注册表（白名单）内。args[0] 为工具名。"""


# ---------------------------------------------------------------------- 参数模型（自定义工具）


class CommandToolParams(BaseModel):
    """command({text})：在游戏聊天框发送文本（/ 开头即游戏命令）。"""

    text: str


class FinishToolParams(BaseModel):
    """finish({result})：结束当前任务并在游戏内播报 result。"""

    result: str


# ---------------------------------------------------------------------- 注册表


@dataclass(frozen=True)
class ToolOutcome:
    """工具执行结果：text 回填 VLM（由循环做长度截断），image 为 screenshot 的图像 bytes。"""

    text: str
    image: bytes | None = None


ToolHandler = Callable[[Any, dict[str, Any]], Awaitable[ToolOutcome]]


@dataclass(frozen=True)
class ToolSpec:
    """单个工具的完整定义：VLM 可见的元数据 + 执行体 + 参数校验模型。"""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    # 客户端侧参数校验模型（None = 不校验，直接透传）
    params_model: type[BaseModel] | None = None


def compact_json(obj: Any) -> str:
    """观测结果 → 紧凑 JSON 文本（回填 VLM 用；不可序列化的对象退化为 repr）。"""
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(obj)


class ToolRegistry:
    """工具注册表：name → ToolSpec；产出 OpenAI function-calling 工具表并统一执行。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    # ------------------------------------------------------------------ 装配

    def register(self, spec: ToolSpec) -> None:
        """注册一个工具（重名覆盖并记 warning——扩展点，M5 分层用）。"""
        if spec.name in self._tools:
            logger.warning("工具 %s 重复注册，后者覆盖前者", spec.name)
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def openai_tools(self) -> list[dict[str, Any]]:
        """产出 OpenAI function-calling 工具表（QwenVLM.chat 的 tools 参数）。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in self._tools.values()
        ]

    # ------------------------------------------------------------------ 执行

    async def execute(self, client: Any, name: str,
                      arguments: Mapping[str, Any] | None) -> ToolOutcome:
        """执行一次工具调用：白名单检查 → 参数校验 → handler。

        - 未注册（白名单外）→ :class:`UnknownToolError`
        - 参数不过 schema → :class:`pydantic.ValidationError`
        - handler 内的 bridge 错误（BridgeError/Timeout）由调用方（循环）翻译成文本
        """
        spec = self._tools.get(name)
        if spec is None:
            raise UnknownToolError(name)
        raw = dict(arguments or {})
        if spec.params_model is not None:
            validated = spec.params_model.model_validate(raw)
            args = validated.model_dump(mode="json", exclude_none=True)
        else:
            args = raw
        return await spec.handler(client, args)


# ---------------------------------------------------------------------- schema 装载


def load_schema_parameters(method: str) -> dict[str, Any]:
    """从冻结产物 ``schema/tools/<method>.json`` 读参数 JSON Schema（剥掉 $schema 记号键）。

    文件不存在 → ``FileNotFoundError``（白名单里的方法必须有冻结 schema，缺了是装配错误）。
    """
    path = SCHEMA_TOOLS_DIR / f"{method}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return {key: value for key, value in data.items() if key != "$schema"}


# ---------------------------------------------------------------------- handlers


def _json_tool(method: str) -> ToolHandler:
    """通用观察/动作工具工厂：bridge 调用结果整体压成紧凑 JSON 文本。"""

    async def handler(client: BridgeClient, args: dict[str, Any]) -> ToolOutcome:
        result = await client.call(method, args)
        return ToolOutcome(compact_json(result))

    return handler


async def _handle_screenshot(client: BridgeClient, args: dict[str, Any]) -> ToolOutcome:
    """screenshot：图像 bytes 放 ToolOutcome.image（循环附进下一轮 user 消息）。"""
    result = await client.call("screenshot", args)
    image_b64 = None
    if isinstance(result, Mapping):
        image_b64 = result.get("image_b64") or result.get("jpeg_b64")
    if not image_b64:
        # 不出图像的异常形态如实回给模型，让它决定重试还是换路
        return ToolOutcome(f"screenshot 未返回图像：{compact_json(result)}")
    try:
        image = base64.b64decode(image_b64)
    except (ValueError, TypeError) as exc:
        return ToolOutcome(f"screenshot 图像解码失败：{exc}；{compact_json(result)}")
    meta = ""
    if isinstance(result, Mapping):
        meta = (f" {result.get('width', '?')}x{result.get('height', '?')}"
                f" {result.get('format', 'jpeg')}")
    return ToolOutcome(f"[图像已附]{meta}", image=image)


async def _handle_command(client: BridgeClient, args: dict[str, Any]) -> ToolOutcome:
    """command：在游戏聊天框发送文本（client 通常是循环的包装客户端，自回显在那里登记）。"""
    await client.command(args["text"])
    return ToolOutcome(f"已发送：{args['text']}")


async def _handle_finish(client: BridgeClient, args: dict[str, Any]) -> ToolOutcome:
    """finish：不做任何事，result 由循环负责在游戏内播报。"""
    return ToolOutcome(args["result"])


# ---------------------------------------------------------------------- 默认注册表


def default_registry() -> ToolRegistry:
    """M3 白名单最小集的注册表（9 个 bridge 工具 + command + finish = 11 个）。"""
    registry = ToolRegistry()
    for method in BRIDGE_WHITELIST:
        parameters = load_schema_parameters(method)
        handler: ToolHandler = (_handle_screenshot if method == "screenshot"
                                else _json_tool(method))
        registry.register(ToolSpec(
            name=method,
            description=TOOL_HINTS.get(method)
            or str(parameters.get("description") or method),
            parameters=parameters,
            handler=handler,
            params_model=TOOL_PARAMS.get(method),
        ))
    registry.register(ToolSpec(
        name=COMMAND_TOOL,
        description=TOOL_HINTS[COMMAND_TOOL],
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要发送的聊天文本（/ 开头即游戏命令）"},
            },
            "required": ["text"],
        },
        handler=_handle_command,
        params_model=CommandToolParams,
    ))
    registry.register(ToolSpec(
        name=FINISH_TOOL,
        description=TOOL_HINTS[FINISH_TOOL],
        parameters={
            "type": "object",
            "properties": {
                "result": {"type": "string", "description": "任务结束语（在游戏聊天里播报）"},
            },
            "required": ["result"],
        },
        handler=_handle_finish,
        params_model=FinishToolParams,
    ))
    return registry


# 参数校验异常的紧凑文本（循环把 ValidationError 翻译成模型可读的回填）
def validation_error_text(name: str, exc: ValidationError) -> str:
    errors = "; ".join(
        f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
        for err in exc.errors(include_url=False)
    )
    return f"工具 {name} 参数校验失败：{errors}"


__all__ = [
    "BRIDGE_WHITELIST",
    "COMMAND_TOOL",
    "FINISH_TOOL",
    "SCHEMA_TOOLS_DIR",
    "TOOL_HINTS",
    "ToolHandler",
    "ToolOutcome",
    "ToolRegistry",
    "ToolSpec",
    "UnknownToolError",
    "compact_json",
    "default_registry",
    "load_schema_parameters",
    "validation_error_text",
]
