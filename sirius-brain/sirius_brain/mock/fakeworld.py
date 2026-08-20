"""T4 假世界 bridge：MockBridgeServer 上叠加可变世界状态，原语离线端到端可测。

维护的状态（构造后测试可直接改写）：
- ``position``：玩家脚底坐标 dict(x,y,z)；假 Baritone 推进它
- ``blocks``：``{(int x, int y, int z) → registry 名}``；input.click 按条件删除
- ``yaw``/``pitch``：lookAt 记录的朝向
- ``submitted``/``looks``/``clicks``：聊天行 / lookAt 目标 / 点击参数的 wire 记录（断言用）

行为映射（未覆盖方法经 ``tool_result`` 钩子回落 MockScript 通用成功）：
- getStats      → 回 position（结构对照 tests/fixtures/two_player_scene.json）
- world.query   → blocks 表按 range 立方扫描 + filter（registry 名或 ``#tag``，内置
                  logs/planks 两张 tag 表），按与玩家距离平方升序，cap 32 + truncated
                  （T1 v1.1 契约的 Python 侧镜像）
- lookAt        → 记录朝向（由目标点反解 yaw/pitch，MC 欧拉角约定）
- command 路径  → BridgeClient.command 的 T→text→ENTER 三连：input.key 开聊天框、
                  input.text 暂存文本、input.key ENTER 提交；``#goto x [y] z`` 启动
                  假 Baritone 协程（每 0.5s 前进 4.3 m/s，到达即停），``#stop`` 停止
- input.click   → 左键且按住达标（hold_ms≥100，25ms tap 挖不掉方块）且目标方块在
                  触及距离（眼位→中心 ≤4.5）且朝向已对准（夹角 ≤30°）→ 从 blocks
                  删除；bedrock 永不可破坏（"挖不破"场景）
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from .server import MockBridgeServer

# ---------------------------------------------------------------------- 常量

#: 假 Baritone 推进节拍（秒）：与 Primitives.poll_interval 同量级，轮询能看到逐格推进
MOVE_INTERVAL = 0.5
#: 步行速度（m/s）：MC 疾走+跳跃的真实均速（2.15 格/节拍）
MOVE_SPEED = 4.3
#: 玩家眼位高度（格）：触及/朝向判定从眼睛出发
EYE_HEIGHT = 1.62
#: 触及距离（格，眼位→方块中心）：与 Primitives.DIG_REACH 同源
REACH = 4.5
#: 朝向对准容差（度）：lookAt 直指方块中心时夹角≈0，30° 吸走浮点误差与"看着附近"
AIM_TOLERANCE_DEG = 30.0
#: 按住时长下限（毫秒）：低于它的点击按 25ms tap 处理——挖不掉任何非即碎方块
MIN_BREAK_HOLD_MS = 100
#: world.query 结果条数上限：与 Java 侧 BLOCKS_CAP 一致
BLOCKS_CAP = 32
#: 永不可破坏的方块（"挖不破"教学场景：工具不足/保护规则的现实对应物）
UNBREAKABLE_BLOCKS = {"minecraft:bedrock"}

# 内置 tag 表（前缀 # 匹配；vanilla logs/planks 两组足够覆盖测试与演示）
_LOG_IDS = {
    "minecraft:oak_log", "minecraft:spruce_log", "minecraft:birch_log",
    "minecraft:jungle_log", "minecraft:acacia_log", "minecraft:dark_oak_log",
    "minecraft:mangrove_log", "minecraft:cherry_log",
    "minecraft:crimson_stem", "minecraft:warped_stem",
}
_PLANK_IDS = {log.replace("_log", "_planks").replace("_stem", "_planks")
              for log in _LOG_IDS}
BLOCK_TAGS: dict[str, set[str]] = {
    "#minecraft:logs": _LOG_IDS,
    "#logs": _LOG_IDS,
    "#minecraft:planks": _PLANK_IDS,
    "#planks": _PLANK_IDS,
}

# BridgeClient.command 编排用的 GLFW 键码（与 bridge/client.py 一致）
GLFW_KEY_T = 84
GLFW_KEY_ENTER = 257


def _normalized_block_id(block_id: str) -> str:
    """补 ``minecraft:`` 前缀（测试里写短名更顺手，wire 上恒为全名）。"""
    return block_id if ":" in block_id else f"minecraft:{block_id}"


def _filter_matches(block_id: str, entry: str) -> bool:
    """单个 filter 条目匹配：registry 名（短名自动补前缀）或 ``#tag``（T1 契约）。"""
    if entry.startswith("#"):
        tag = entry[1:]
        if ":" not in tag:
            tag = f"minecraft:{tag}"
        return block_id in BLOCK_TAGS.get(f"#{tag}", set())
    return block_id == _normalized_block_id(entry)


class FakeWorldBridge(MockBridgeServer):
    """可变世界 mock：getStats/world.query/lookAt/command(假 Baritone)/input.click 有状态。"""

    def __init__(
        self,
        *,
        position: dict[str, float] | None = None,
        blocks: dict[tuple[int, int, int], str] | None = None,
        move_speed: float = MOVE_SPEED,
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        super().__init__(host=host, port=port)
        self.position: dict[str, float] = dict(position or {"x": 0.0, "y": 64.0, "z": 0.0})
        self.blocks: dict[tuple[int, int, int], str] = {
            tuple(int(v) for v in key): _normalized_block_id(value)
            for key, value in (blocks or {}).items()
        }
        #: 假 Baritone 步行速度（m/s）；0 = 冻结世界（超时/看门狗场景用）
        self.move_speed = move_speed
        self.yaw = 0.0
        self.pitch = 0.0
        # wire 记录（断言用）
        self.submitted: list[str] = []          # ENTER 提交的聊天行（含 # 命令）
        self.looks: list[tuple[float, float, float]] = []
        self.clicks: list[dict[str, Any]] = []
        # 聊天框状态机（BridgeClient.command 的 T→text→ENTER 三连）
        self._chat_open = False
        self._pending_text: str | None = None
        self._mover_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ 分发

    async def tool_result(self, method: str, params: dict[str, Any]) -> Any:
        if method == "getStats":
            return self._result_get_stats()
        if method == "world.query":
            return self._result_world_query(params)
        if method == "lookAt":
            return self._result_look_at(params)
        if method == "input.click":
            return self._result_click(params)
        if method == "input.key":
            return self._result_key(params)
        if method == "input.text":
            return self._result_text(params)
        return await super().tool_result(method, params)  # 未覆盖 → 通用成功

    # ------------------------------------------------------------------ 感知

    def _result_get_stats(self) -> dict[str, Any]:
        """getStats：结构对照 two_player_scene.json（位置换成活的）。"""
        return {
            "in_game": True,
            "health": 20.0,
            "food": 20,
            "saturation": 5.0,
            "air": 300,
            "xp_level": 0,
            "xp_progress": 0.0,
            "position": dict(self.position),
            "dimension": "minecraft:overworld",
            "game_mode": "survival",
            "effects": [],
            "alive": True,
        }

    def _result_world_query(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("type") == "entities":
            return {"entities": [], "count": 0}  # 假世界不模拟其他实体
        range_ = float(params.get("range", 16))
        radius = math.ceil(range_)
        px, py, pz = self.position["x"], self.position["y"], self.position["z"]
        cx, cy, cz = int(math.floor(px)), int(math.floor(py)), int(math.floor(pz))
        filters = params.get("filter") or None
        matches: list[tuple[float, dict[str, Any]]] = []
        for (x, y, z), block_id in self.blocks.items():
            # 立方扫描（与 Java scanBlocks 同口径：中心块坐标 ± ceil(range) 每轴）
            if abs(x - cx) > radius or abs(y - cy) > radius or abs(z - cz) > radius:
                continue
            if filters and not any(_filter_matches(block_id, f) for f in filters):
                continue
            dist_sq = ((x + 0.5 - px) ** 2 + (y + 0.5 - py) ** 2 + (z + 0.5 - pz) ** 2)
            matches.append((dist_sq, {"x": x, "y": y, "z": z, "block": block_id}))
        # 命中后按与玩家距离平方升序（T1 契约），cap 32 + truncated
        matches.sort(key=lambda item: item[0])
        truncated = len(matches) > BLOCKS_CAP
        top = [block for _, block in matches[:BLOCKS_CAP]]
        return {"blocks": top, "count": len(top), "truncated": truncated}

    def _result_look_at(self, params: dict[str, Any]) -> dict[str, Any]:
        """lookAt：记录朝向（由目标反解 yaw/pitch，MC 欧拉角：yaw 0=+Z、pitch 正=低头）。"""
        tx, ty, tz = float(params["x"]), float(params["y"]), float(params["z"])
        self.looks.append((tx, ty, tz))
        ex, ey, ez = self._eye()
        dx, dy, dz = tx - ex, ty - ey, tz - ez
        horizontal = math.hypot(dx, dz)
        self.yaw = math.degrees(math.atan2(-dx, dz))
        self.pitch = -math.degrees(math.atan2(dy, horizontal)) if horizontal else (-90.0 if dy > 0 else 90.0)
        return {
            "in_game": True, "looked": True,
            "yaw": round(self.yaw, 1), "pitch": round(self.pitch, 1),
            "distance": round(math.sqrt(dx * dx + dy * dy + dz * dz), 1),
        }

    # ------------------------------------------------------------------ 输入

    def _result_key(self, params: dict[str, Any]) -> dict[str, Any]:
        code = int(params.get("code", -1))
        if code == GLFW_KEY_T:
            self._chat_open = True
        elif code == GLFW_KEY_ENTER:
            self._chat_open = False
            line, self._pending_text = self._pending_text, None
            if line is not None:
                self.submitted.append(line)
                if line.startswith("#"):
                    self._handle_baritone(line)
        return {"injected": True, "key": f"glfw:{code}", "glfw_key": code,
                "modifiers": list(params.get("modifiers") or []),
                "duration_ms": int(params.get("duration_ms", 0)),
                "release_scheduled": True, "screen_open": self._chat_open}

    def _result_text(self, params: dict[str, Any]) -> dict[str, Any]:
        self._pending_text = str(params.get("string", ""))
        return {"delivered": True, "length": len(self._pending_text),
                "delivered_all": True}

    def _result_click(self, params: dict[str, Any]) -> dict[str, Any]:
        self.clicks.append(dict(params))
        button = int(params.get("button", -1))
        hold_ms = int(params.get("hold_ms") or 0)
        broke: list[dict[str, Any]] = []
        if button == 0 and hold_ms >= MIN_BREAK_HOLD_MS:
            target = self._aimed_block()
            if target is not None:
                block_id = self.blocks[target]
                if block_id not in UNBREAKABLE_BLOCKS:
                    del self.blocks[target]
                    broke.append({"x": target[0], "y": target[1], "z": target[2],
                                  "block": block_id})
        result: dict[str, Any] = {"clicked": True, "button": button,
                                  "count": int(params.get("count", 1)),
                                  "screen_open": False}
        if broke:
            result["broke"] = broke
        return result

    # ------------------------------------------------------------------ 假 Baritone

    def _handle_baritone(self, line: str) -> None:
        """聊天行以 # 开头 → 客户端侧命令（不达服务器）：这里只模拟 goto/stop。"""
        tokens = line.split()
        try:
            if tokens[0] == "#goto" and len(tokens) in (3, 4):
                if len(tokens) == 4:
                    x, y, z = float(tokens[1]), float(tokens[2]), float(tokens[3])
                else:  # 两参形式：y 由寻路器落地面，这里保持当前高度（平地世界）
                    x, z = float(tokens[1]), float(tokens[2])
                    y = self.position["y"]
                self._start_mover(x, y, z)
            elif tokens[0] == "#stop":
                self._stop_mover()
        except (ValueError, IndexError):
            pass  # 参数烂掉 → 静默忽略（真实客户端也只是回一行用法提示）

    def _start_mover(self, x: float, y: float, z: float) -> None:
        self._stop_mover()
        task = asyncio.create_task(self._mover(x, y, z))
        self._track(task)  # 登记 _pending：close() 时统一取消，不留悬挂任务
        self._mover_task = task

    def _stop_mover(self) -> None:
        if self._mover_task is not None and not self._mover_task.done():
            self._mover_task.cancel()
        self._mover_task = None

    async def _mover(self, tx: float, ty: float, tz: float) -> None:
        """假 Baritone：每节拍朝目标推进 move_speed×间隔 格，到达即停。"""
        step = max(0.0, self.move_speed) * MOVE_INTERVAL
        try:
            while True:
                await asyncio.sleep(MOVE_INTERVAL)
                px, py, pz = self.position["x"], self.position["y"], self.position["z"]
                dx, dz = tx - px, tz - pz
                horizontal = math.hypot(dx, dz)
                if horizontal <= step:
                    self.position = {"x": tx, "y": ty, "z": tz}  # 到达（含 step=0 且已到位）
                    return
                if step <= 0:
                    continue  # 冻结世界（move_speed=0）：位置不动，循环空转等 #stop
                ratio = step / horizontal
                self.position = {
                    "x": px + dx * ratio,
                    "y": py + (ty - py) * min(1.0, ratio),  # 高度按比例滑向目标（平地即不变）
                    "z": pz + dz * ratio,
                }
        except asyncio.CancelledError:
            raise  # #stop / 新 #goto / close() 的取消路径

    # ------------------------------------------------------------------ 瞄准判定

    def _eye(self) -> tuple[float, float, float]:
        return self.position["x"], self.position["y"] + EYE_HEIGHT, self.position["z"]

    def _aimed_block(self) -> tuple[int, int, int] | None:
        """视线指向的方块：触及距离内、与视线夹角最小且 ≤ 容差的那个（None = 没瞄上）。"""
        ex, ey, ez = self._eye()
        yaw_rad = math.radians(self.yaw)
        pitch_rad = math.radians(self.pitch)
        # MC 视线方向：yaw 0=+Z（南）、90=-X（西）；pitch 正=低头
        direction = (-math.sin(yaw_rad) * math.cos(pitch_rad),
                     -math.sin(pitch_rad),
                     math.cos(yaw_rad) * math.cos(pitch_rad))
        best: tuple[float, tuple[int, int, int]] | None = None
        for (x, y, z) in self.blocks:
            to = (x + 0.5 - ex, y + 0.5 - ey, z + 0.5 - ez)
            length = math.sqrt(to[0] ** 2 + to[1] ** 2 + to[2] ** 2)
            if length > REACH or length == 0:
                continue
            cos_angle = (direction[0] * to[0] + direction[1] * to[1] + direction[2] * to[2]) / length
            angle = math.degrees(math.acos(max(-1.0, min(1.0, cos_angle))))
            if angle <= AIM_TOLERANCE_DEG and (best is None or angle < best[0]):
                best = (angle, (x, y, z))
        return best[1] if best is not None else None


__all__ = [
    "BLOCKS_CAP",
    "BLOCK_TAGS",
    "EYE_HEIGHT",
    "FakeWorldBridge",
    "MIN_BREAK_HOLD_MS",
    "MOVE_INTERVAL",
    "MOVE_SPEED",
    "REACH",
    "UNBREAKABLE_BLOCKS",
]
