"""Mock bridge 真实 WebSocket 回环测试。spec §8.2 / §10.1 M0。

不 mock websockets 库本身：每条用例在系统分配的随机端口上起真实服务，用
websockets 客户端走完整回环。项目未安装 pytest-asyncio，故以 asyncio.run()
驱动场景协程（等效的事件循环管线，验收口径不变）。
"""

import asyncio
import json
import time

import pytest
from websockets.asyncio.client import connect

from sirius_brain.mock import (
    MockBridgeServer,
    MockScript,
    ScriptedTask,
    ScriptedToolResponse,
    load_replay,
)
from sirius_brain.protocol import (
    CapabilitiesListRequest,
    CapabilitiesListResponse,
    EventLevel,
    NotificationFrame,
    TaskFinishedFrame,
    TaskFrame,
    ToolCallRequest,
    ToolCallResponse,
    TOOL_PARAMS,
)


class TestHandshake:
    def test_capabilities_roundtrip(self):
        """连接 → capabilities/list 往返：能力清单来自 T1 TOOL_PARAMS + 协议版本。"""

        async def scenario():
            async with MockBridgeServer(port=0) as server:
                async with connect(server.url) as ws:
                    await ws.send(CapabilitiesListRequest(id="c1").model_dump_json())
                    resp = CapabilitiesListResponse.model_validate_json(await ws.recv())
                    assert resp.id == "c1"
                    assert resp.error is None
                    assert resp.protocol_version == "1.0"
                    assert {cap.name for cap in resp.result} == set(TOOL_PARAMS)
                    # 每项能力带参数 JSON Schema（版本协商用）
                    assert all(cap.input_schema for cap in resp.result)

        asyncio.run(scenario())


class TestToolCalls:
    def test_scripted_result_with_delay(self):
        """剧本工具调用：延迟回包 + result 原样返回。"""

        async def scenario():
            script = MockScript(tools={
                "getStats": ScriptedToolResponse(result={"health": 6, "food": 20},
                                                 delay_ms=80),
            })
            async with MockBridgeServer(script, port=0) as server:
                async with connect(server.url) as ws:
                    req = ToolCallRequest(id="r1", method="getStats")
                    t0 = time.perf_counter()
                    await ws.send(req.model_dump_json())
                    resp = ToolCallResponse.model_validate_json(await ws.recv())
                    elapsed = time.perf_counter() - t0
            assert resp.id == "r1"
            assert resp.error is None
            assert resp.result == {"health": 6, "food": 20}
            assert elapsed >= 0.08

        asyncio.run(scenario())

    def test_scripted_error(self):
        """剧本错误分支：error 对象透传。"""

        async def scenario():
            script = MockScript(tools={
                "input.text": ScriptedToolResponse(
                    error={"code": -32000, "message": "GUI 未打开"}),
            })
            async with MockBridgeServer(script, port=0) as server:
                async with connect(server.url) as ws:
                    await ws.send(ToolCallRequest(
                        id="r2", method="input.text", params={"string": "hi"},
                    ).model_dump_json())
                    resp = ToolCallResponse.model_validate_json(await ws.recv())
            assert resp.id == "r2"
            assert resp.result is None
            assert resp.error is not None
            assert resp.error.code == -32000
            assert resp.error.message == "GUI 未打开"

        asyncio.run(scenario())

    def test_unscripted_tool_generic_ok(self):
        """能力清单内但未编排的方法：回通用成功（echo 参数）。"""

        async def scenario():
            async with MockBridgeServer(port=0) as server:
                async with connect(server.url) as ws:
                    await ws.send(ToolCallRequest(
                        id="r3", method="look", params={"yaw": 10.0, "pitch": -5.0},
                    ).model_dump_json())
                    resp = ToolCallResponse.model_validate_json(await ws.recv())
            assert resp.id == "r3"
            assert resp.error is None
            assert resp.result == {
                "ok": True, "method": "look", "echo": {"yaw": 10.0, "pitch": -5.0},
            }

        asyncio.run(scenario())

    def test_unknown_method_not_found(self):
        async def scenario():
            async with MockBridgeServer(port=0) as server:
                async with connect(server.url) as ws:
                    await ws.send(ToolCallRequest(id="r4", method="warp").model_dump_json())
                    resp = ToolCallResponse.model_validate_json(await ws.recv())
            assert resp.id == "r4"
            assert resp.error is not None
            assert resp.error.code == -32601

        asyncio.run(scenario())

    def test_invalid_params(self):
        """参数不通过 JSON Schema：-32602 + 错误明细。"""

        async def scenario():
            async with MockBridgeServer(port=0) as server:
                async with connect(server.url) as ws:
                    await ws.send(ToolCallRequest(
                        id="r5", method="input.text", params={},
                    ).model_dump_json())
                    resp = ToolCallResponse.model_validate_json(await ws.recv())
            assert resp.id == "r5"
            assert resp.error is not None
            assert resp.error.code == -32602
            assert resp.error.data  # 校验错误明细列表非空

        asyncio.run(scenario())


class TestTaskFrames:
    def test_task_finished_roundtrip_default(self):
        """task → task_finished：默认立即 ok，task_id 原样回传（含特殊字符）。"""
        special_id = 'T-42/綺麗 💎 "quoted" & <tag>'

        async def scenario():
            async with MockBridgeServer(port=0) as server:
                async with connect(server.url) as ws:
                    await ws.send(TaskFrame(task="挖一组铁矿石",
                                            task_id=special_id).model_dump_json())
                    finished = TaskFinishedFrame.model_validate_json(await ws.recv())
            assert finished.task_id == special_id
            assert finished.status == "ok"

        asyncio.run(scenario())

    def test_task_scripted_failed_with_delay(self):
        rules = [
            ScriptedTask(match="挖矿", status="failed", text="镐子断了", delay_ms=150),
        ]

        async def scenario():
            async with MockBridgeServer(MockScript(task_rules=rules), port=0) as server:
                async with connect(server.url) as ws:
                    t0 = time.perf_counter()
                    await ws.send(TaskFrame(task="挖矿到 16 层",
                                            task_id="T-43").model_dump_json())
                    finished = TaskFinishedFrame.model_validate_json(await ws.recv())
                    elapsed = time.perf_counter() - t0
            assert finished.task_id == "T-43"
            assert finished.status == "failed"
            assert finished.text == "镐子断了"
            assert elapsed >= 0.14

        asyncio.run(scenario())

    def test_out_of_order_completion_attributed_by_task_id(self):
        """乱序完成：先派慢任务再派快任务，完成帧按 id 正确归属。spec §8.2。"""
        rules = [
            ScriptedTask(match="慢", status="ok", text="slow", delay_ms=250),
            ScriptedTask(match="快", status="ok", text="fast", delay_ms=20),
        ]

        async def scenario():
            async with MockBridgeServer(MockScript(task_rules=rules), port=0) as server:
                async with connect(server.url) as ws:
                    await ws.send(TaskFrame(task="慢任务", task_id="slow-1").model_dump_json())
                    await ws.send(TaskFrame(task="快任务", task_id="fast-1").model_dump_json())
                    first = TaskFinishedFrame.model_validate_json(await ws.recv())
                    second = TaskFinishedFrame.model_validate_json(await ws.recv())
            assert first.task_id == "fast-1" and first.text == "fast"
            assert second.task_id == "slow-1" and second.text == "slow"

        asyncio.run(scenario())


class TestInvalidFrames:
    @pytest.mark.parametrize(
        "raw,expect_code,expect_id",
        [
            ("это не json", -32700, ""),           # 非 JSON
            ("[1, 2, 3]", -32600, ""),             # JSON 但非对象
            ('{"type": "request", "id": "r9"}', -32600, "r9"),   # 缺 method
            ('{"type": "task", "task": "x"}', -32600, ""),       # 缺 task_id
            ('{"type": "response", "id": "x"}', -32600, "x"),    # 后端不应发的帧
            ('{"type": "task", "task": 42, "task_id": "t"}', -32600, ""),  # 字段类型错
        ],
    )
    def test_invalid_frame_gets_error(self, raw, expect_code, expect_id):
        async def scenario():
            async with MockBridgeServer(port=0) as server:
                async with connect(server.url) as ws:
                    await ws.send(raw)
                    resp = ToolCallResponse.model_validate_json(await ws.recv())
            assert resp.error is not None
            assert resp.error.code == expect_code
            assert resp.id == expect_id

        asyncio.run(scenario())


class TestNotifications:
    def test_push_seq_monotonic_with_levels(self):
        """主动推送：seq 每连接单调递增，事件分级注入 data['level']。"""

        async def scenario():
            async with MockBridgeServer(port=0) as server:
                async with connect(server.url) as ws:
                    await server.push_notification("fire", {"source": "creeper"},
                                                   EventLevel.CRITICAL)
                    await server.push_notification("hunger")  # 默认 INFO
                    await server.push_notification("gui_change", {}, EventLevel.WARNING)
                    frames = [NotificationFrame.model_validate_json(await ws.recv())
                              for _ in range(3)]
            assert [f.event for f in frames] == ["fire", "hunger", "gui_change"]
            assert [f.seq for f in frames] == [0, 1, 2]
            assert frames[0].data == {"source": "creeper", "level": "CRITICAL"}
            assert frames[1].data["level"] == "INFO"
            assert frames[2].data["level"] == "WARNING"


class TestReplay:
    def _write_jsonl(self, path, lines):
        path.write_text("\n".join(json.dumps(line, ensure_ascii=False)
                                  for line in lines), encoding="utf-8")

    def test_replay_sequential_mode(self, tmp_path):
        """按序回放：delay_ms 控制节奏，事件按序到达且 seq 递增。"""
        recording = tmp_path / "frames.jsonl"
        self._write_jsonl(recording, [
            {"event": "chat", "data": {"text": "hi"}, "level": "INFO", "delay_ms": 60},
            {"event": "health", "data": {"health": 6}, "level": "CRITICAL", "delay_ms": 30},
            {"type": "notification", "event": "weather", "data": {}, "seq": 99},
        ])

        async def scenario():
            async with MockBridgeServer(port=0) as server:
                async with connect(server.url) as ws:
                    server.start_replay(load_replay(recording), speed=10)
                    frames = [NotificationFrame.model_validate_json(await ws.recv())
                              for _ in range(3)]
            assert [f.event for f in frames] == ["chat", "health", "weather"]
            assert [f.seq for f in frames] == [0, 1, 2]  # 录制旧 seq(99) 被重编号
            assert frames[1].data["level"] == "CRITICAL"

        asyncio.run(scenario())

    def test_replay_timestamp_mode(self, tmp_path):
        """按时间戳回放：以首条 timestamp 为零点做墙钟调度，顺序保持。"""
        recording = tmp_path / "timeline.jsonl"
        self._write_jsonl(recording, [
            {"event": "e0", "timestamp": 100.0},
            {"event": "e1", "timestamp": 101.0},
            {"event": "e2", "timestamp": 101.5},
        ])

        async def scenario():
            async with MockBridgeServer(port=0) as server:
                async with connect(server.url) as ws:
                    server.start_replay(load_replay(recording), speed=100)
                    frames = [NotificationFrame.model_validate_json(await ws.recv())
                              for _ in range(3)]
            assert [f.event for f in frames] == ["e0", "e1", "e2"]
            assert [f.seq for f in frames] == [0, 1, 2]

        asyncio.run(scenario())

    def test_replay_rejects_non_notification_line(self, tmp_path):
        recording = tmp_path / "bad.jsonl"
        self._write_jsonl(recording, [{"type": "task", "task": "x", "task_id": "t"}])
        with pytest.raises(ValueError, match="notification"):
            load_replay(recording)


class TestMockScriptModel:
    """剧本模型纯逻辑（不起服务）。"""

    def test_task_outcome_first_match_wins(self):
        script = MockScript(task_rules=[
            ScriptedTask(match="挖", status="ok", text="rule-1"),
            ScriptedTask(match=None, status="superseded", text="catch-all"),
            ScriptedTask(match="挖矿", status="failed", text="rule-3"),
        ])
        assert script.task_outcome("挖一组矿").text == "rule-1"
        assert script.task_outcome("合成工作台").text == "catch-all"
        # 无任何规则命中 → 兜底剧本（默认立即 ok）
        assert MockScript().task_outcome("任意").status == "ok"
        assert MockScript().task_outcome("任意").delay_ms == 0

    def test_from_json_file(self, tmp_path):
        """JSON 文件剧本：工具/任务规则均按 pydantic 校验解析。"""
        scene = {
            "protocol_version": "0.9-mock",
            "tools": {"screenshot": {"result": {"tier": "full"}, "delay_ms": 10}},
            "task_rules": [{"match": "挖矿", "status": "failed", "text": "镐子断了",
                            "delay_ms": 3000}],
        }
        path = tmp_path / "scene.json"
        path.write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
        script = MockScript.from_json_file(path)
        assert script.protocol_version == "0.9-mock"
        assert script.tool_response("screenshot").result == {"tier": "full"}
        outcome = script.task_outcome("挖矿到 16 层")
        assert outcome.status == "failed" and outcome.text == "镐子断了"

    def test_capabilities_override_for_version_negotiation(self):
        """裁剪能力清单可模拟弱身体（ capabilities 协商测试场景）。"""
        script = MockScript.model_validate({
            "capabilities": [{"name": "screenshot", "version": "1.0"}],
        })
        assert script.capability_names() == {"screenshot"}
