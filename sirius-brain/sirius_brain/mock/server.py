"""Mock bridge server（假身体）：大脑开发用的可脚本化 WebSocket JSON 服务。

spec §8.2（帧格式/工具集/事件分级/NEKO 帧五态）、§2.1（asyncio + websockets）、
§10.1 M0（大脑轨全程对 mock body 开发）。

帧处理规则（全部复用 T1 协议模型校验，不重复定义协议类型）：
- 合法 JSON 但帧模型校验失败        → ToolCallResponse(error code=-32600 invalid frame)
- 非 JSON                          → ToolCallResponse(error code=-32700 parse error)
- request 但 method 不在能力清单     → ToolCallResponse(error code=-32601 method not found)
- request 参数不通过 JSON Schema    → ToolCallResponse(error code=-32602 invalid params)
- request capabilities/list        → CapabilitiesListResponse（能力清单 + 协议版本）
- request 其他方法                  → 按脚本延迟回 result 或 error（未编排回通用成功）
- task（NEKO 帧）                   → 按脚本延迟回 task_finished，task_id 原样回传

事件推送（Mod → 后端，一等公民）：push_notification() 广播到所有活跃连接，
seq 每连接单调递增（从 0 起）；事件分级注入 data["level"]（T1 帧模型无 level 字段，规格语义在 data 载荷里表达）。
"""

import asyncio
import json
import time
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ValidationError
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from sirius_brain.protocol import (
    CapabilitiesListRequest,
    CapabilitiesListResponse,
    EventLevel,
    NotificationFrame,
    TaskFinishedFrame,
    TaskFrame,
    ToolCallError,
    ToolCallRequest,
    ToolCallResponse,
    TOOL_PARAMS,
)

from .replay import ReplayEntry
from .script import MockScript

# JSON-RPC 风格错误码（与协议测试中 -32602 的用法一致）
PARSE_ERROR = -32700
INVALID_FRAME = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


def _validation_summary(exc: ValidationError) -> list[dict[str, Any]]:
    """提取可 JSON 序列化的校验错误摘要（loc/msg/type）。"""
    return [
        {"loc": list(err["loc"]), "msg": str(err["msg"]), "type": err["type"]}
        for err in exc.errors(include_url=False)
    ]


class _ClientState:
    """单个客户端连接的状态：seq 计数器 + 发送锁（保证 seq 与发送顺序一致）。"""

    def __init__(self, ws: ServerConnection) -> None:
        self.ws = ws
        self.seq = 0
        self.send_lock = asyncio.Lock()


class MockBridgeServer:
    """可脚本化的假身体 WebSocket 服务。

    用法::

        async with MockBridgeServer(MockScript(), port=0) as server:
            async with connect(server.url) as ws: ...
    """

    def __init__(
        self,
        script: MockScript | None = None,
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        self.script = script or MockScript()
        self.host = host
        self.port = port
        self._server: Any = None
        self._connections: set[_ClientState] = set()
        self._pending: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ 生命周期

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    async def start(self) -> "MockBridgeServer":
        """启动监听。port=0 时由系统分配随机端口，启动后 self.port 为实际端口。"""
        self._server = await serve(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def close(self) -> None:
        """关闭：取消后台任务（延迟回包/回放）、断开连接、关停监听。"""
        for task in self._pending:
            task.cancel()
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
        self._pending.clear()
        for conn in list(self._connections):
            await conn.ws.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> "MockBridgeServer":
        return await self.start()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ 事件推送

    async def push_notification(
        self,
        event: str,
        data: dict[str, Any] | None = None,
        level: EventLevel | None = EventLevel.INFO,
    ) -> None:
        """向所有活跃连接广播 notification 帧；seq 每连接单调递增。spec §8.2。"""
        payload = dict(data or {})
        if level is not None:
            payload.setdefault("level", EventLevel(level).value)
        for conn in list(self._connections):
            frame = NotificationFrame(
                event=event, data=payload, timestamp=time.time(), seq=conn.seq
            )
            conn.seq += 1
            await self._send(conn, frame)

    def start_replay(
        self, entries: Iterable[ReplayEntry], speed: float = 1.0
    ) -> asyncio.Task:
        """启动后台回放任务（不阻塞）。时间轴见 replay 模块 docstring。"""
        task = asyncio.create_task(self._replay_coro(list(entries), speed))
        self._track(task)
        return task

    async def _replay_coro(self, entries: list[ReplayEntry], speed: float) -> None:
        if not entries:
            return
        loop = asyncio.get_running_loop()
        start = loop.time()
        # 任一条目带 timestamp 即进入"按时间戳"模式：以首条 timestamp 为零点做墙钟调度
        t0 = next((e.timestamp for e in entries if e.timestamp is not None), None)
        for entry in entries:
            if t0 is not None and entry.timestamp is not None:
                wait = (entry.timestamp - t0) / speed - (loop.time() - start)
            else:
                wait = entry.delay_ms / 1000.0 / speed
            if wait > 0:
                await asyncio.sleep(wait)
            if self._connections:  # 无连接时丢帧，回放不中断
                await self.push_notification(entry.event, entry.data, entry.level)

    # ------------------------------------------------------------------ 连接处理

    async def _handle(self, ws: ServerConnection) -> None:
        conn = _ClientState(ws)
        self._connections.add(conn)
        try:
            async for raw in ws:
                await self._on_message(conn, raw)
        finally:
            self._connections.discard(conn)

    async def _on_message(self, conn: _ClientState, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            msg: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            await self._reply_error(conn, "", PARSE_ERROR, f"not valid JSON: {exc.msg}")
            return
        if not isinstance(msg, dict):
            await self._reply_error(conn, "", INVALID_FRAME, "帧必须是 JSON 对象")
            return

        frame_type = msg.get("type")
        if frame_type == "request":
            await self._on_request(conn, msg)
        elif frame_type == "task":
            await self._on_task(conn, msg)
        else:
            await self._reply_error(
                conn, _best_effort_id(msg), INVALID_FRAME,
                f"未知帧 type={frame_type!r}（后端→Mod 仅支持 request/task）",
            )

    # ---- request 分支：capabilities/list 协商 + 工具调用 ----

    async def _on_request(self, conn: _ClientState, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if method == "capabilities/list":
            try:
                CapabilitiesListRequest.model_validate(msg)
            except ValidationError as exc:
                await self._reply_error(
                    conn, _best_effort_id(msg), INVALID_FRAME,
                    "capabilities/list 请求帧校验失败", _validation_summary(exc),
                )
                return
            await self._send(conn, CapabilitiesListResponse(
                id=msg["id"],
                result=self.script.capabilities,
                protocol_version=self.script.protocol_version,
            ))
            return

        try:
            req = ToolCallRequest.model_validate(msg)
        except ValidationError as exc:
            await self._reply_error(
                conn, _best_effort_id(msg), INVALID_FRAME,
                "请求帧校验失败", _validation_summary(exc),
            )
            return

        if req.method not in self.script.capability_names():
            await self._reply_error(
                conn, req.id, METHOD_NOT_FOUND,
                f"未知方法 {req.method!r}（可用能力见 capabilities/list）",
            )
            return

        params_model = TOOL_PARAMS.get(req.method)
        if params_model is not None:
            try:
                params_model.model_validate(req.params)
            except ValidationError as exc:
                await self._reply_error(
                    conn, req.id, INVALID_PARAMS,
                    f"方法 {req.method} 参数校验失败", _validation_summary(exc),
                )
                return

        scripted = self.script.tool_response(req.method)
        if scripted is not None:
            await asyncio.sleep(scripted.delay_ms / 1000.0)
            await self._send(conn, ToolCallResponse(
                id=req.id, result=scripted.result, error=scripted.error,
            ))
        else:
            # 能力清单内但未编排：回通用成功（echo 参数便于测试断言）
            await self._send(conn, ToolCallResponse(
                id=req.id,
                result={"ok": True, "method": req.method, "echo": req.params},
            ))

    # ---- task 分支：NEKO 兼容帧，延迟回 task_finished ----

    async def _on_task(self, conn: _ClientState, msg: dict[str, Any]) -> None:
        try:
            frame = TaskFrame.model_validate(msg)
        except ValidationError as exc:
            await self._reply_error(
                conn, _best_effort_id(msg), INVALID_FRAME,
                "task 帧校验失败", _validation_summary(exc),
            )
            return

        outcome = self.script.task_outcome(frame.task)
        # task_id 原样回传（out-of-order 完成靠 id 归属，不按完成序）。spec §8.2
        task_id, status, text = frame.task_id, outcome.status, outcome.text

        async def finish() -> None:
            await asyncio.sleep(outcome.delay_ms / 1000.0)
            try:
                await self._send(conn, TaskFinishedFrame(
                    status=status, task_id=task_id, text=text,
                ))
            except ConnectionClosed:
                pass  # 客户端已断开，丢弃回包

        self._track(asyncio.create_task(finish()))

    # ------------------------------------------------------------------ 工具

    def _track(self, task: asyncio.Task) -> None:
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _send(self, conn: _ClientState, frame: BaseModel) -> None:
        async with conn.send_lock:
            await conn.ws.send(frame.model_dump_json())

    async def _reply_error(
        self,
        conn: _ClientState,
        id_: str,
        code: int,
        message: str,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        await self._send(conn, ToolCallResponse(
            id=id_, error=ToolCallError(code=code, message=message, data=errors),
        ))


def _best_effort_id(msg: dict[str, Any]) -> str:
    """非法帧里尽力取 id 以便客户端配对；取不到回空串。"""
    candidate = msg.get("id")
    return candidate if isinstance(candidate, str) else ""
