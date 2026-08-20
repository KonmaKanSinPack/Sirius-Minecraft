"""M3.5 原语模块测试（T2×T4 联跑）：Primitives 对 FakeWorldBridge 全离线端到端。

- 世界用 FakeWorldBridge（T4 假世界）：可变 blocks + 假 Baritone（#goto/#stop）+
  朝向/触及判定的 input.click——原语的每条契约路径都能离线复现
- 全部 mock 服务端绑定随机空闲端口（port=0，与 test_bridge_client/test_mock_bridge
  同口径）：8765 留给真机 bridge，测试绝不与之抢端口/发生连接
- 项目未安装 pytest-asyncio，异步场景以 asyncio.run() 驱动（与 test_agent_loop 同口径）
- client.command 的收尾等待压到 20ms（0.4+0.3s 的 T→text→ENTER 时序保留——
  FakeWorldBridge 的聊天框状态机依赖它）；看门狗/静默常量用 monkeypatch 压短，
  不为测试放宽生产代码
"""

import asyncio
import math

from sirius_brain.agent import Primitives
from sirius_brain.agent import primitives as primitives_module
from sirius_brain.bridge import BridgeClient
from sirius_brain.mock import FakeWorldBridge

ORIGIN = {"x": 0.0, "y": 64.0, "z": 0.0}


# ---------------------------------------------------------------------- 测试基建


def fast_commands(client: BridgeClient) -> None:
    """把 client.command 的 settle 压到 20ms（离线 mock 不需要等命令在服务器生效）。"""
    real_command = client.command

    async def fast(text: str, settle: float = 0.5, timeout=None):  # noqa: ANN001, ANN202
        return await real_command(text, settle=0.02, timeout=timeout)

    client.command = fast  # type: ignore[method-assign]


async def make_pair(server: FakeWorldBridge) -> BridgeClient:
    await server.start()
    client = BridgeClient(server.url)
    await client.connect()
    fast_commands(client)
    return client


def dist_xy(server: FakeWorldBridge, x: float, z: float) -> float:
    return math.hypot(server.position["x"] - x, server.position["z"] - z)


async def flip_flag_after(flag: dict, delay: float) -> None:
    """delay 秒后把 flag["stop"] 置 True（取消测试的急停触发器）。"""
    await asyncio.sleep(delay)
    flag["stop"] = True


# ---------------------------------------------------------------------- FakeWorldBridge 行为（单元）


class TestFakeWorldBehavior:
    def test_world_query_filter_range_and_truncation(self):
        """filter 按 registry 名/#tag 匹配；range 立方扫描（每轴 ±ceil(range)）；cap 32。"""

        async def main() -> None:
            # 40 根云杉原木铺在 7×7 网格上（全部落在 range 16 的立方扫描内）→ 验证 cap；
            # 近处橡木命中 #logs、远处橡木被 range 排除、石头被 filter 排除
            near_cells = [(x, 64, z) for x in range(1, 8) for z in range(1, 8)]
            blocks = {pos: "spruce_log" for pos in near_cells[:40]}
            blocks[(2, 64, -3)] = "oak_log"   # range 3 立方内 → #logs 命中
            blocks[(50, 64, 0)] = "oak_log"   # 远处 → range 排除
            blocks[(1, 64, -1)] = "stone"     # 立方内但非 logs → filter 排除
            server = FakeWorldBridge(port=0, position=dict(ORIGIN), blocks=blocks)
            client = await make_pair(server)
            try:
                by_name = await client.call(
                    "world.query", {"type": "blocks", "range": 16.0,
                                    "filter": ["spruce_log"]})
                assert by_name["count"] == 32 and by_name["truncated"] is True
                assert all(b["block"] == "minecraft:spruce_log" for b in by_name["blocks"])
                # 命中按与玩家距离升序（T1 契约）：最近的在前
                dists = [math.hypot(b["x"] + 0.5, b["z"] + 0.5) for b in by_name["blocks"]]
                assert dists == sorted(dists)

                by_tag = await client.call(
                    "world.query", {"type": "blocks", "range": 3.0, "filter": ["#logs"]})
                # 立方每轴 ±3：9 格云杉 + 1 格橡木；石头与远处橡木都不在结果里
                assert {(b["x"], b["y"], b["z"]) for b in by_tag["blocks"]} == \
                    {(x, 64, z) for x in (1, 2, 3) for z in (1, 2, 3)} | {(2, 64, -3)}
                assert {b["block"] for b in by_tag["blocks"]} == \
                    {"minecraft:spruce_log", "minecraft:oak_log"}
            finally:
                await client.close()
                await server.close()

        asyncio.run(main())

    def test_tap_does_not_break_held_click_does(self):
        """25ms tap 挖不掉方块；hold_ms 达标且瞄准则挖掉（FakeWorldBridge 现实语义）。"""

        async def main() -> None:
            server = FakeWorldBridge(port=0, position={"x": 4.5, "y": 64.0, "z": 2.5},
                                     blocks={(3, 64, 2): "spruce_log"})
            client = await make_pair(server)
            try:
                await client.call("lookAt", {"x": 3.5, "y": 64.5, "z": 2.5})
                await client.call("input.click", {"button": 0})  # 旧契约的 25ms tap
                assert (3, 64, 2) in server.blocks
                await client.call("input.click", {"button": 0, "hold_ms": 600})
                assert (3, 64, 2) not in server.blocks
            finally:
                await client.close()
                await server.close()

        asyncio.run(main())


# ---------------------------------------------------------------------- walk_to


class TestWalkTo:
    def test_arrives(self):
        """常规到达：#goto 两参形式上 wire，位置推进到目标，成功话术带最终坐标。"""

        async def main() -> None:
            server = FakeWorldBridge(port=0, position=dict(ORIGIN))
            client = await make_pair(server)
            try:
                outcome = await Primitives(client).walk_to(10.0, 8.0)
                assert outcome.text.startswith("已走到")
                assert "(10, 64, 8)" in outcome.text
                assert "#goto 10 8" in server.submitted
                assert dist_xy(server, 10.0, 8.0) <= 0.01  # 假 Baritone 精确到达
            finally:
                await client.close()
                await server.close()

        asyncio.run(main())

    def test_timeout_sends_stop(self):
        """冻结世界（move_speed=0）→ 超时：发 #stop + "同参数重发可续走"健康超时话术。"""

        async def main() -> None:
            server = FakeWorldBridge(port=0, position=dict(ORIGIN), move_speed=0.0)
            client = await make_pair(server)
            try:
                outcome = await Primitives(client).walk_to(10.0, 8.0, timeout=1.0)
                assert "超时" in outcome.text
                assert "同参数重发" in outcome.text  # 建议续走而非重试（Numen 话术）
                assert "#stop" in server.submitted
                assert dist_xy(server, 10.0, 8.0) > 5.0  # 没到
            finally:
                await client.close()
                await server.close()

        asyncio.run(main())

    def test_stall_resends_goto_once(self, monkeypatch):
        """看门狗：距离无进展只重发一次 #goto（近重试档），之后等超时收尾。"""
        monkeypatch.setattr(primitives_module, "WALK_STALL_SECONDS", 0.3)

        async def main() -> None:
            server = FakeWorldBridge(port=0, position=dict(ORIGIN), move_speed=0.0)
            client = await make_pair(server)
            try:
                outcome = await Primitives(client).walk_to(10.0, 8.0, timeout=2.0)
                assert "超时" in outcome.text
                assert server.submitted.count("#goto 10 8") == 2  # 首发 + 恰一次重发
                assert server.submitted.count("#stop") == 1
            finally:
                await client.close()
                await server.close()

        asyncio.run(main())

    def test_cancel_stops_and_reports_position(self):
        """协作式取消：微步检查点断出 → #stop 上 wire → 中止文案带当前坐标。"""

        async def main() -> None:
            server = FakeWorldBridge(port=0, position=dict(ORIGIN))
            client = await make_pair(server)
            flag = {"stop": False}
            try:
                flipper = asyncio.create_task(flip_flag_after(flag, 1.5))
                outcome = await Primitives(client).walk_to(
                    60.0, 0.0, cancel=lambda: flag["stop"])
                await flipper
                assert "已中止" in outcome.text
                assert "#stop" in server.submitted
                assert "当前位于" in outcome.text  # 取消话术带当前坐标（Numen 契约）
                assert 0.5 < server.position["x"] < 50.0  # 走了一段但没到
            finally:
                await client.close()
                await server.close()

        asyncio.run(main())


# ---------------------------------------------------------------------- 界面屏障（T0b 教训）


class ScreenStubClient:
    """无服务器最小 client（Primitives 只要求 call/command）：getGuiState 按 screens
    序列回放（最后一条重复）；fail_gui=True 时 getGuiState 抛错（屏障降级路径）。"""

    def __init__(self, screens: list[dict], *, fail_gui: bool = False):
        self.screens = list(screens)
        self.fail_gui = fail_gui
        self.commands: list[str] = []
        self.gui_calls = 0

    async def call(self, method, params=None):  # noqa: ANN001, ANN202
        if method == "getGuiState":
            self.gui_calls += 1
            if self.fail_gui:
                raise RuntimeError("getGuiState 不可用")
            return self.screens[min(self.gui_calls - 1, len(self.screens) - 1)]
        if method == "getStats":
            return {"in_game": True, "position": {"x": 0.0, "y": 64.0, "z": 0.0}}
        return {}

    async def command(self, text: str):
        self.commands.append(text)


class TestScreenBarrier:
    def test_waits_for_loading_screen_to_clear(self):
        """T0b 根因复现：加载屏未消失时先等（轮询 getGuiState）再发 #goto，命令不丢。"""

        async def main() -> None:
            stub = ScreenStubClient([
                {"screen_open": True, "in_game": True,
                 "screen_class": "LevelLoadingScreen"},   # 第 1 次查：屏还在
                {"screen_open": False},                     # 第 2 次查：已消失
            ])
            outcome = await Primitives(stub, poll_interval=0.02).walk_to(1.0, 0.0)
            assert stub.gui_calls == 2          # 屏障等了一轮才放行
            assert stub.commands == ["#goto 1 0"]  # 屏消失后命令才发出（T0b：之前会丢）
            assert outcome.text.startswith("已走到")

        asyncio.run(main())

    def test_barrier_timeout_blocks_goto(self, monkeypatch):
        """屏一直不消失：等到上限即教学式失败（先处理界面），绝不盲发 #goto。"""
        monkeypatch.setattr(primitives_module, "WALK_SCREEN_BARRIER_TIMEOUT", 0.05)

        async def main() -> None:
            stub = ScreenStubClient([
                {"screen_open": True, "in_game": True,
                 "screen_class": "LevelLoadingScreen"},
            ])
            outcome = await Primitives(stub, poll_interval=0.02).walk_to(10.0, 8.0)
            assert "界面被 LevelLoadingScreen 占用" in outcome.text
            assert "处理界面" in outcome.text          # 建议行动：先处理界面
            assert "重发 walkTo" in outcome.text
            assert stub.commands == []                 # 没有发出任何命令

        asyncio.run(main())

    def test_gui_query_failure_does_not_block(self):
        """getGuiState 查询失败：屏障视同无界面放行（防丢命令的措施不该反过来卡行走）。"""

        async def main() -> None:
            stub = ScreenStubClient([], fail_gui=True)
            outcome = await Primitives(stub, poll_interval=0.02).walk_to(1.0, 0.0)
            assert stub.gui_calls == 1                # 确实查过一次（失败）
            assert "#goto 1 0" in stub.commands       # 放行
            assert outcome.text.startswith("已走到")

        asyncio.run(main())


# ---------------------------------------------------------------------- dig_block


class TestDigBlock:
    def test_dig_success(self):
        """触及范围内：lookAt 中心 → hold 600ms 左键 → 方块消失，成功话术带 registry 名。"""

        async def main() -> None:
            server = FakeWorldBridge(port=0, position={"x": 4.5, "y": 64.0, "z": 2.5},
                                     blocks={(3, 64, 2): "spruce_log"})
            client = await make_pair(server)
            try:
                outcome = await Primitives(client).dig_block(3, 64, 2)
                assert outcome.text == "已挖掉 minecraft:spruce_log（3,64,2）"
                assert (3, 64, 2) not in server.blocks
                assert server.looks[-1] == (3.5, 64.5, 2.5)      # 看向方块中心
                assert server.clicks[-1] == {"button": 0, "hold_ms": 600}
            finally:
                await client.close()
                await server.close()

        asyncio.run(main())

    def test_dig_too_far_teaches_walk_first(self):
        """超触及：教学式失败（先 walkTo 旁边），不动键鼠、方块原样保留。
        感知范围外的目标不能被误报成"已空"——同样教先走位。"""

        async def main() -> None:
            server = FakeWorldBridge(port=0, position=dict(ORIGIN),
                                     blocks={(10, 64, 10): "spruce_log",
                                             (100, 64, 100): "spruce_log"})
            client = await make_pair(server)
            try:
                outcome = await Primitives(client).dig_block(10, 64, 10)
                assert "超出触及范围" in outcome.text
                assert "walkTo" in outcome.text  # 下一步建议：先走过去
                assert "minecraft:spruce_log" in outcome.text  # 看得见就报是什么方块
                assert (10, 64, 10) in server.blocks
                assert not server.looks and not server.clicks  # 未盲挖

                far = await Primitives(client).dig_block(100, 64, 100)  # 感知范围外
                assert "远超触及与感知范围" in far.text
                assert "walkTo" in far.text
                assert not server.looks and not server.clicks
            finally:
                await client.close()
                await server.close()

        asyncio.run(main())

    def test_dig_already_gone_is_success(self):
        """目标已空：幂等成功（此前已挖掉或本就是空气）。"""

        async def main() -> None:
            server = FakeWorldBridge(port=0, position={"x": 4.5, "y": 64.0, "z": 2.5},
                                     blocks={(6, 64, 2): "spruce_log"})
            client = await make_pair(server)
            try:
                outcome = await Primitives(client).dig_block(3, 64, 2)  # 这里没有方块
                assert "已不存在" in outcome.text
                assert not server.clicks
            finally:
                await client.close()
                await server.close()

        asyncio.run(main())

    def test_dig_unbreakable_teaches_after_8_segments(self, monkeypatch):
        """挖不破（bedrock）：8 段后教学式失败，不无限空挖。"""
        monkeypatch.setattr(primitives_module, "DIG_SETTLE", 0.02)

        async def main() -> None:
            server = FakeWorldBridge(port=0, position={"x": 4.5, "y": 64.0, "z": 2.5},
                                     blocks={(3, 64, 2): "bedrock"})
            client = await make_pair(server)
            try:
                outcome = await Primitives(client).dig_block(3, 64, 2)
                assert "无法破坏" in outcome.text
                assert "遮挡" in outcome.text or "工具不足" in outcome.text
                assert (3, 64, 2) in server.blocks
                assert len(server.clicks) == 8  # 恰好 8 段就放弃
            finally:
                await client.close()
                await server.close()

        asyncio.run(main())


# ---------------------------------------------------------------------- collect_block


class TestCollectBlock:
    def test_full_chain_with_tag_filter(self):
        """组合场景：collect 3 块 #logs（spruce×2 + oak×1）完整链路——
        query(filter=#tag) → 最近 → walk_to 邻近 → dig_block，循环到收满。"""

        async def main() -> None:
            server = FakeWorldBridge(
                port=0,
                position=dict(ORIGIN),
                blocks={(6, 64, 0): "spruce_log", (10, 64, 4): "oak_log",
                        (14, 64, 8): "spruce_log"})
            client = await make_pair(server)
            try:
                outcome = await Primitives(client).collect_block(["#logs"], 3)
                assert outcome.text == "已挖到 3/3 个 #logs"
                assert not server.blocks  # 三块全挖掉
                gotos = [line for line in server.submitted if line.startswith("#goto")]
                assert len(gotos) >= 3    # 每块一次走位
            finally:
                await client.close()
                await server.close()

        asyncio.run(main())

    def test_partial_collect_is_success(self):
        """部分收：2/5 后范围内清空 → 仍算成功，话术说明"已无更多"。"""

        async def main() -> None:
            server = FakeWorldBridge(port=0, position=dict(ORIGIN),
                                     blocks={(6, 64, 0): "spruce_log",
                                             (12, 64, 0): "spruce_log"})
            client = await make_pair(server)
            try:
                outcome = await Primitives(client).collect_block(["spruce_log"], 5)
                assert "已挖到 2/5" in outcome.text
                assert "范围内已无更多" in outcome.text
                assert not server.blocks
            finally:
                await client.close()
                await server.close()

        asyncio.run(main())

    def test_none_found_is_teaching_failure(self):
        """空范围：教学式失败——确认 ID（含 #tag 写法）或走近些；不发起任何走位。"""

        async def main() -> None:
            server = FakeWorldBridge(port=0, position=dict(ORIGIN),
                                     blocks={(3, 64, 2): "stone"})
            client = await make_pair(server)
            try:
                outcome = await Primitives(client).collect_block(["spruce_log"], 1)
                assert "未找到" in outcome.text
                assert "#tag" in outcome.text  # 写法提示
                assert not [line for line in server.submitted if line.startswith("#goto")]
                assert (3, 64, 2) in server.blocks  # 石头无辜
            finally:
                await client.close()
                await server.close()

        asyncio.run(main())
