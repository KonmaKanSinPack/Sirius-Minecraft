"""M3.5 任务级复合动作原语：意图进、结果出（Numen 式契约），执行下沉确定性代码。

背景（session 2026-08-20）：M3 真机闭环"不智能"——动作粒度太低，VLM 被迫当小脑，
砍树 22 步耗尽预算。本模块把"走到/挖掉/收集 N 个"这类高频复合意图下沉为
一次原语调用：同步阻塞期间零 VLM 调用，LLM 只做意图层决策。

设计要点：
- **同步阻塞 + 协作式取消**（非 Numen 式异步受理）：AgentLoop 本就串行执行工具，
  同步原语让 60s 行走只占 1 个 tool call 位。急停经 ``cancel: Callable[[], bool]``
  在微步循环（poll_interval≈0.5s）每步检查，保证 ≤1s 生效；触发后按场景收尾
  （walk 发 ``#stop`` 停 Baritone），并返回带当前坐标的中止文案
- **结果话术即契约**（Numen 手段）：成功带数字（走到哪/挖了几个）、失败带下一步
  建议（先 walkTo 邻近 / 同参数重发可续走 / 确认 ID 写法）、取消带当前坐标——
  VLM 读文本就能自救，不需要额外结构化通道
- **世界复核靠 world.query**：getStats 读不到脚下以外 的方块，一切"目标还在不在"
  的判定都走 world.query（T1 后支持 filter，按与玩家距离升序返回）
- 本模块自包含、不碰 loop.py / tools.py 注册表（T3 另行接入）；client 只要求
  具备 BridgeClient 的 call()/command() 接口（测试可注入 mock）
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .tools import ToolOutcome

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------- 常量
# 取值理由集中说明（调参先读注释；Numen 参照：MoveToCompanionTask/GoToThenDoTask）

#: walk_to 默认超时（秒）：步行 4.3 m/s 时 120s ≈ 500 格，覆盖 Baritone 常规寻路
WALK_TIMEOUT = 120.0
#: walk_to 到达判定：与目标水平距离 ≤2 格即算到达（Baritone #goto 本就停在目标附近 1-2 格，
#: 抠到 0.5 格会被碰撞箱/地形起伏反复"差一点"）
WALK_ARRIVE_DIST = 2.0
#: 距离无进展看门狗（秒）：Baritone 绕路/卡跳沿时距离可能暂时不减，15s 足以区分
#: "绕路中"与"真卡死"；触发后只重发一次 #goto（Numen 近重试档，MoveToCompanionTask）
WALK_STALL_SECONDS = 15.0
#: 发 #goto 前的界面屏障等待上限（秒）。T0b 教训（reports/M3.5-T0b.md）：quickPlay
#: 入世后世界加载屏未消失时，T 键打不开聊天框，#goto 静默丢失——等屏消失再发令
WALK_SCREEN_BARRIER_TIMEOUT = 10.0

#: dig_block 默认超时（秒）：8 段挖掘（见 DIG_MAX_SEGMENTS）约 9s，30s 上限留足余量
DIG_TIMEOUT = 30.0
#: 挖掘触及距离（格，脚底坐标到方块中心）：MC 生存交互距离 4.5（与 FakeWorldBridge
#: 的 eye→center ≤4.5 判定同源）；超出则先移动（Numen GoToThenDoTask 的 OUT_OF_REACH 教学）
DIG_REACH = 4.5
#: 单段挖掘的按住时长（毫秒）：原木 hardness≈2，徒手/铁斧约 0.3-0.6s 破坏，
#: 600ms 是"按住不放直到破坏或松手"的稳妥值（25ms tap 挖不掉任何非即碎方块）
DIG_CLICK_HOLD_MS = 600
#: 每段挖掘后等服务端方块移除同步回来的静默（秒）
DIG_SETTLE = 0.5
#: 连续挖掘段数上限：8 段 ×(0.6+0.5)s ≈ 9s 仍不破 → 判定挖不动（被遮挡/工具不足/
#: 保护规则），给教学式失败而不是无限空挖
DIG_MAX_SEGMENTS = 8

#: collect_block 的扫描半径（格）：与 bridge world.query 的 MAX_RANGE 对齐（Java 侧
#: 超过 64 直接 -32602，本地常量避免无谓往返）
COLLECT_RANGE = 64.0
#: collect_block 走位目标：目标方块旁 ±1.5 格的邻位点（不到方块本身上，也不出触及范围）
COLLECT_NEAR_OFFSET = 1.5

#: 取消回调类型：返回 True 即请求中止（急停检查点）
CancelFlag = Callable[[], bool] | None


@dataclass(frozen=True)
class _StepResult:
    """原语内部微步骤结果（public 方法把它包装成 ToolOutcome 文本）。

    ok=False 时 text 已是"失败 + 下一步建议"的教学文案，可直接上抛给 VLM。
    """

    ok: bool
    text: str


def _fmt(value: float) -> str:
    """坐标 → 命令参数文本：10.0→"10"、10.5→"10.5"（避免 #goto 10.000000 之类噪声）。"""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-") else "0"


def _dist2(ax: float, ay: float, az: float, bx: float, by: float, bz: float) -> float:
    return (ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2


class Primitives:
    """任务级复合动作原语集合（walk_to / dig_block / collect_block）。

    用法（T3 接入后由工具注册表包装；当前可独立驱动）::

        prims = Primitives(client, poll_interval=0.5)
        outcome = await prims.walk_to(103.0, -198.0, cancel=loop.stop_requested)
    """

    def __init__(self, client: Any, *, poll_interval: float = 0.5) -> None:
        #: BridgeClient（或测试 mock）：要求 call(method, params) / command(text)
        self.client = client
        #: 微步轮询间隔（秒）：也是取消检查的粒度（≤1s 急停的来源）
        self.poll_interval = poll_interval

    # ------------------------------------------------------------------ walk_to

    async def walk_to(self, x: float, z: float, y: float | None = None,
                      timeout: float = WALK_TIMEOUT,
                      cancel: CancelFlag = None) -> ToolOutcome:
        """走到 (x, z)（y 可选，给了走三维目标）：Baritone #goto + 轮询到位。

        - 成功："已走到 (x,y,z)，距目标 n 格"
        - 15s 无进展重发一次 #goto；超时发 #stop + "同参数重发可续走"
        - 取消：发 #stop + 当前坐标
        """
        result = await self._walk_to(x, z, y=y, timeout=timeout, cancel=cancel)
        return ToolOutcome(result.text)

    async def _walk_to(self, x: float, z: float, *, y: float | None = None,
                       timeout: float = WALK_TIMEOUT,
                       cancel: CancelFlag = None) -> _StepResult:
        # 0) 界面屏障（T0b 教训）：加载/覆盖屏未消失时聊天框打不开，#goto 会静默
        #    丢失——先等屏消失再发令（collect_block 的走位也经此路径，同样受保护）
        barrier = await self._wait_screen_clear(cancel=cancel)
        if barrier is not None:
            return barrier
        # y 缺省走两参形式（Baritone 自动落地面），给了走三参形式
        goto_cmd = (f"#goto {_fmt(x)} {_fmt(y)} {_fmt(z)}" if y is not None
                    else f"#goto {_fmt(x)} {_fmt(z)}")
        position = await self._position()
        if position is None:
            return _StepResult(
                False, "无法开始行走：getStats 未返回有效位置；先观察（getStats）确认已在游戏中")
        await self.client.command(goto_cmd)
        logger.info("walk_to → %s（从 %.1f,%.1f,%.1f 出发）", goto_cmd, *position)

        started = time.monotonic()
        best_dist = math.inf
        last_progress = started
        resent = False  # 看门狗只重发一次（Numen 近重试档）
        while True:
            if cancel is not None and cancel():
                return await self._abort_walk(x, z)
            position = await self._position()
            now = time.monotonic()
            if position is not None:
                px, py, pz = position
                dist = math.hypot(px - x, pz - z)  # 到达判定只看水平距离（y 由寻路器解）
                logger.debug("walk_to 轮询：位置 %.1f,%.1f,%.1f 距目标 %.2f 格", px, py, pz, dist)
                if dist <= WALK_ARRIVE_DIST:
                    logger.info("walk_to 到达：%.1f,%.1f,%.1f（距目标 %.2f 格）", px, py, pz, dist)
                    return _StepResult(
                        True, f"已走到 ({_fmt(px)}, {_fmt(py)}, {_fmt(pz)})，距目标 {dist:.1f} 格")
                if dist < best_dist - 0.05:  # 有实质进展（0.05 格吸走浮点/原地踏步抖动）
                    best_dist = dist
                    last_progress = now
                elif not resent and now - last_progress > WALK_STALL_SECONDS:
                    resent = True
                    last_progress = now
                    logger.info("walk_to %.0fs 无进展（距目标 %.1f 格），重发一次 %s",
                                WALK_STALL_SECONDS, dist, goto_cmd)
                    await self.client.command(goto_cmd)
            if now - started > timeout:
                # 健康超时话术（Numen MoveToCompanionTask）：路程仍在推进但超预算 → 续走可行
                await self.client.command("#stop")
                where = (f"{_fmt(position[0])}, {_fmt(position[1])}, {_fmt(position[2])}"
                         if position else "未知")
                remain = f"{math.hypot(position[0] - x, position[2] - z):.1f} 格" if position else "未知距离"
                logger.warning("walk_to 超时（%.0fs）：仍在 %s，距目标 %s，已发 #stop",
                               timeout, where, remain)
                return _StepResult(
                    False,
                    f"行走超时（{timeout:.0f}s）：现在位于 ({where})，距目标仍 {remain}。"
                    f"路程仍在推进但超出本次预算；同参数重发 walkTo 可续走，或改用更近的途经点分段走")
            await asyncio.sleep(self.poll_interval)

    async def _abort_walk(self, x: float, z: float) -> _StepResult:
        """取消行走：发 #stop 停 Baritone，报当前坐标（续走/换路的决策依据）。"""
        await self.client.command("#stop")
        position = await self._position()
        logger.info("walk_to 被取消（目标 %.1f,%.1f），已发 #stop，当前 %s", x, z, position)
        if position is None:
            return _StepResult(False, "行走已中止（#stop 已发送），当前坐标不可用")
        px, py, pz = position
        dist = math.hypot(px - x, pz - z)
        return _StepResult(
            False, f"行走已中止（#stop 已发送）：当前位于 ({_fmt(px)}, {_fmt(py)}, {_fmt(pz)})，"
                   f"距目标 {dist:.1f} 格；需要继续时同参数重发 walkTo 即可续走")

    async def _wait_screen_clear(self, *, cancel: CancelFlag = None,
                                 timeout: float | None = None) -> _StepResult | None:
        """发 #goto 前的界面屏障：轮询等待任意已打开的 screen 消失（T0b 教训）。

        判定按 getGuiState 的 ``screen_open``（非 null screen 就等）：具体类名里哪些算
        "加载/覆盖类"不可靠（模组可自定义 screen 类），宁可多等一轮也不丢命令。
        返回 None = 已无界面，可以发令；返回 _StepResult = 失败文案（等待超时/被取消）。
        """
        if timeout is None:
            timeout = WALK_SCREEN_BARRIER_TIMEOUT
        started = time.monotonic()
        seen: str | None = None  # 最近一次观测到的占用界面（解除时留档日志用）
        while True:
            screen_class = await self._screen_class()
            if screen_class is None:
                if seen is not None:
                    logger.info("walk_to 屏障解除：等待 %.1fs 后 %s 已消失",
                                time.monotonic() - started, seen)
                return None
            seen = screen_class
            if cancel is not None and cancel():
                logger.info("walk_to 屏障等待中被取消：界面仍被 %s 占用", screen_class)
                return _StepResult(
                    False, f"行走已中止：等待界面 {screen_class} 消失期间收到停止指令"
                           f"（尚未开始行走，未发 #goto）")
            if time.monotonic() - started > timeout:
                logger.warning("walk_to 屏障等待 %.0fs 超时：界面仍被 %s 占用，不发 #goto",
                               timeout, screen_class)
                return _StepResult(
                    False, f"界面被 {screen_class} 占用（等待 {timeout:.0f}s 未消失），"
                           f"此时发命令会丢失；先用 getGuiState 查看并处理界面"
                           f"（必要时按 ESC 关闭），再重发 walkTo")
            await asyncio.sleep(self.poll_interval)

    async def _screen_class(self) -> str | None:
        """getGuiState → 当前占用屏幕的类名（无屏 → None）。

        调用失败视同无屏放行（屏障是尽力而为的防丢命令措施，不该反过来阻塞行走）。
        """
        try:
            result = await self.client.call("getGuiState")
        except Exception as exc:  # noqa: BLE001
            logger.warning("getGuiState 调用失败（屏障视同无界面）：%s", exc)
            return None
        if isinstance(result, dict) and result.get("screen_open"):
            return str(result.get("screen_class") or "unknown")
        return None

    # ------------------------------------------------------------------ dig_block

    async def dig_block(self, x: int, y: int, z: int,
                        timeout: float = DIG_TIMEOUT,
                        cancel: CancelFlag = None) -> ToolOutcome:
        """挖掉 (x,y,z) 的方块：复核存在 → 触及/朝向/按住点击循环 → 复核消失。

        - 已空 → 直接成功（幂等）
        - 距离 >4.5 → "先 walkTo 到它旁边"教学失败（Numen GoToThenDoTask 话术）
        - 8 段未破 → "被遮挡/工具不足/保护规则"screenshot 观察或换目标
        """
        result = await self._dig_block(x, y, z, timeout=timeout, cancel=cancel)
        return ToolOutcome(result.text)

    async def _dig_block(self, x: int, y: int, z: int, *,
                         timeout: float = DIG_TIMEOUT,
                         cancel: CancelFlag = None) -> _StepResult:
        # 0) 取自身坐标：触及判定与"查询半径要盖住目标"的复核都依赖它
        position = await self._position()
        if position is None:
            return _StepResult(
                False, f"无法挖掘 ({x},{y},{z})：getStats 未返回有效位置；先观察（getStats）确认状态")
        dist = math.sqrt(_dist2(position[0], position[1], position[2], x + 0.5, y + 0.5, z + 0.5))
        # 1) 存在性复核：getStats 读不到脚下以外的方块，用 world.query 的立方扫描拿
        #    目标坐标的现方块。查询半径取 min(距离+1.5, 64)——盖住目标（"看不到"≠
        #    "不存在"，远处未命中应教学先走位，而不是误报"已空"）；后续每段挖掘的
        #    复核复用同一半径
        scan_range = min(dist + 1.5, COLLECT_RANGE)
        target = await self._block_at(x, y, z, range_=scan_range)
        if target is None:
            if dist > COLLECT_RANGE:
                logger.info("dig_block 远超感知范围：%.1f 格 > %.0f，放弃并建议先走位",
                            dist, COLLECT_RANGE)
                return _StepResult(
                    False, f"目标 ({x},{y},{z}) 距离 {dist:.1f} 格，远超触及与感知范围"
                           f"（{COLLECT_RANGE:.0f} 格）；先 walkTo 到它附近再挖")
            return _StepResult(
                True, f"目标方块 ({x},{y},{z}) 已不存在（此前已挖掉或本就是空气），无需再挖")
        block_id = target["block"]
        logger.info("dig_block 开始：(%d,%d,%d) 的 %s（距 %.1f 格）", x, y, z, block_id, dist)

        # 2) 触及距离检查：太远不给"盲挖"，教学先走过去（旅行归 walkTo）
        if dist > DIG_REACH:
            logger.info("dig_block 距离不足：%.1f 格 > %.1f，放弃并建议先走位", dist, DIG_REACH)
            return _StepResult(
                False, f"目标 ({x},{y},{z}) 的 {block_id} 距离 {dist:.1f} 格，超出触及范围（{DIG_REACH} 格）；"
                       f"先 walkTo 到它旁边（±1.5 格）再挖")

        # 3) 挖掘循环：看准 → 按住左键 → 等同步 → 复核消失
        started = time.monotonic()
        for segment in range(1, DIG_MAX_SEGMENTS + 1):
            if cancel is not None and cancel():
                px, py, pz = position  # 步骤 0 已保证非空，循环内刷新也只会更不空
                logger.info("dig_block 被取消：(%d,%d,%d) 第 %d 段后中止", x, y, z, segment)
                return _StepResult(
                    False, f"挖掘已中止：当前位于 ({_fmt(px)}, {_fmt(py)}, {_fmt(pz)})，"
                           f"目标 ({x},{y},{z}) 的 {block_id} 尚未破坏")
            await self.client.call("lookAt", {"x": x + 0.5, "y": y + 0.5, "z": z + 0.5})
            await self.client.call("input.click", {"button": 0, "hold_ms": DIG_CLICK_HOLD_MS})
            await asyncio.sleep(DIG_SETTLE)
            if await self._block_at(x, y, z, range_=scan_range) is None:
                logger.info("dig_block 完成：(%d,%d,%d) 的 %s（第 %d 段）", x, y, z, block_id, segment)
                return _StepResult(True, f"已挖掉 {block_id}（{x},{y},{z}）")
            if time.monotonic() - started > timeout:
                break
            position = await self._position() or position  # 挖掘间隙也刷新坐标（取消话术用）
        logger.warning("dig_block %d 段仍未破坏 (%d,%d,%d) 的 %s", DIG_MAX_SEGMENTS, x, y, z, block_id)
        return _StepResult(
            False, f"无法破坏 ({x},{y},{z}) 的 {block_id}：连续 {DIG_MAX_SEGMENTS} 段挖掘后仍在"
                   f"（可能被遮挡/工具不足/保护规则）；建议 screenshot 观察四周，或换一个目标")

    # ------------------------------------------------------------------ collect_block

    async def collect_block(self, block_ids: list[str], count: int,
                            cancel: CancelFlag = None) -> ToolOutcome:
        """收集 count 个指定方块：query 最近 → 走到旁边 → 挖掉，循环到收满或清空。

        收尾契约（Numen MineCompanionTask）：
        - destroyed ≥ count → "已挖到 n/count 个 <ids>"
        - 0 < destroyed < count → "已挖到 n/count；范围内已无更多…"（仍算成功）
        - destroyed == 0 → 失败："范围 64 格内未找到 <ids>；确认 ID（含 #tag 写法）或走近些"
        """
        result = await self._collect_block(block_ids, count, cancel=cancel)
        return ToolOutcome(result.text)

    async def _collect_block(self, block_ids: list[str], count: int,
                             cancel: CancelFlag = None) -> _StepResult:
        label = ",".join(block_ids)
        if count < 1:
            return _StepResult(False, f"collect 数量必须 ≥1，收到 {count}")
        destroyed = 0
        stop_reason = ""  # 收尾文案分叉：query 空 / 走位失败 / 挖掘失败 / 取消
        while destroyed < count:
            if cancel is not None and cancel():
                stop_reason = "已中止"
                break
            # 1) 感知：filter 过滤后的候选按与玩家距离升序（T1 契约），取最近
            blocks = await self._query_blocks(block_ids)
            if not blocks:
                stop_reason = "范围内已无更多" if destroyed else ""
                break
            position = await self._position()
            if position is None:
                stop_reason = "getStats 不可用"
                break
            px, py, pz = position
            nearest = min(blocks, key=lambda b: _dist2(px, py, pz,
                                                       b["x"] + 0.5, b["y"] + 0.5, b["z"] + 0.5))
            bx, by, bz = nearest["x"], nearest["y"], nearest["z"]
            # 2) 走位：目标方块旁 ±1.5 格的四个邻点里挑离自己最近的（少走冤枉路，
            #    落点必然在触及范围内）
            candidates = [(bx + COLLECT_NEAR_OFFSET, bz), (bx - COLLECT_NEAR_OFFSET, bz),
                          (bx, bz + COLLECT_NEAR_OFFSET), (bx, bz - COLLECT_NEAR_OFFSET)]
            wx, wz = min(candidates, key=lambda c: math.hypot(px - c[0], pz - c[1]))
            logger.debug("collect_block：%s 最近候选 (%d,%d,%d)，走位到 (%s,%s)",
                         label, bx, by, bz, _fmt(wx), _fmt(wz))
            walk = await self._walk_to(wx, wz, cancel=cancel)
            if not walk.ok:
                stop_reason = f"走位未成功（{walk.text}）"
                break
            # 3) 挖掘（cancel 已透传；挖掉才计数）
            dig = await self._dig_block(bx, by, bz, cancel=cancel)
            if dig.ok:
                destroyed += 1
                logger.info("collect_block 进度：%d/%d 个 %s", destroyed, count, label)
            else:
                stop_reason = f"挖掘受阻（{dig.text}）"
                break

        # 收尾契约（三种话术见 docstring）
        if destroyed >= count:
            logger.info("collect_block 完成：%d/%d 个 %s", destroyed, count, label)
            return _StepResult(True, f"已挖到 {destroyed}/{count} 个 {label}")
        if destroyed > 0:
            return _StepResult(
                True, f"已挖到 {destroyed}/{count} 个 {label}；{stop_reason or '范围内已无更多'}，"
                      f"可接受这个结果，或走远后再试")
        if stop_reason:  # 一个都没挖到且不是"没找到"：把微步骤的教学建议原样上抛
            return _StepResult(False, f"未挖到任何 {label}：{stop_reason}")
        return _StepResult(
            False, f"范围 {COLLECT_RANGE:.0f} 格内未找到 {label}；请确认方块 ID "
                   f"（支持 #tag 写法，如 #minecraft:logs），或走近一些再试")

    # ------------------------------------------------------------------ 感知辅助

    async def _position(self) -> tuple[float, float, float] | None:
        """getStats → 脚底坐标（不可用时 None；调用方决定失败文案）。"""
        try:
            result = await self.client.call("getStats")
        except Exception as exc:  # noqa: BLE001 —— 感知失败降级为 None，由上层翻译
            logger.warning("getStats 调用失败：%s", exc)
            return None
        if not isinstance(result, dict) or not result.get("in_game"):
            return None
        pos = result.get("position")
        if not isinstance(pos, dict):
            return None
        try:
            return float(pos["x"]), float(pos["y"]), float(pos["z"])
        except (KeyError, TypeError, ValueError):
            return None

    async def _query_blocks(self, filters: list[str] | None = None,
                            range_: float = COLLECT_RANGE) -> list[dict[str, Any]]:
        """world.query(type=blocks, filter?) → 方方块列表（坐标 int、block 为 registry 名）。

        bridge 侧已按与玩家距离升序返回（T1 契约），这里只做形态防御。
        """
        params: dict[str, Any] = {"type": "blocks", "range": range_}
        if filters:
            params["filter"] = list(filters)
        try:
            result = await self.client.call("world.query", params)
        except Exception as exc:  # noqa: BLE001
            logger.warning("world.query 调用失败（filter=%s）：%s", filters, exc)
            return []
        if isinstance(result, dict) and result.get("truncated"):
            logger.debug("world.query 结果被截断（cap 32）：最近方块仍可信，远处不全")
        blocks = result.get("blocks") if isinstance(result, dict) else None
        if not isinstance(blocks, list):
            return []
        return [b for b in blocks if isinstance(b, dict)
                and all(isinstance(b.get(key), (int, float)) for key in ("x", "y", "z"))
                and isinstance(b.get("block"), str)]

    async def _block_at(self, x: int, y: int, z: int, *,
                        range_: float) -> dict[str, Any] | None:
        """目标坐标的现方块（None = 扫描范围内没有）。调用方负责保证查询半径盖住目标。"""
        for block in await self._query_blocks(range_=range_):
            if (block["x"], block["y"], block["z"]) == (x, y, z):
                return block
        return None


__all__ = [
    "CancelFlag",
    "Primitives",
    "COLLECT_NEAR_OFFSET",
    "COLLECT_RANGE",
    "DIG_CLICK_HOLD_MS",
    "DIG_MAX_SEGMENTS",
    "DIG_REACH",
    "DIG_SETTLE",
    "DIG_TIMEOUT",
    "WALK_ARRIVE_DIST",
    "WALK_SCREEN_BARRIER_TIMEOUT",
    "WALK_STALL_SECONDS",
    "WALK_TIMEOUT",
]
