# -*- coding: utf-8 -*-
"""M3.5 T7：挖后拾取真机验收探针（不依赖 VLM）。

前提：游戏带**新 bridge jar（entities 载荷含 item 注册名 + count）**启动。本脚本：
  1. 实体载荷冒烟：world.query entities 的 item 实体带 item/count 字段
  2. 主场景：collect_block(['minecraft:oak_log'], 1)——挖前记基线掉落 → 挖后断言
     目标掉落物实体消失 + 话术含"已捡起"
  3. pickup=False 对照（可选，第 2 棵树）：挖后掉落物留在地上
用法：python m35_t7_pickup_probe.py [2|3]   # 只跑指定项（默认 1+2）
token 从实例 config/sirius_bridge.toml 现读，不写入任何输出。
"""
import asyncio
import math
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "sirius-brain"))

from sirius_brain.agent.primitives import Primitives  # noqa: E402
from sirius_brain.bridge.client import BridgeClient  # noqa: E402
from sirius_brain.bridge.config import BridgeConfig  # noqa: E402

TOML = ROOT / ".minecraft/versions/1.21.1-Sirius/config/sirius_bridge.toml"
TARGET_LOG = "minecraft:oak_log"
MIN_HEALTH = 10.0

RESULTS: list[tuple[str, bool, str]] = []


def record(item: str, ok: bool, evidence: str) -> None:
    RESULTS.append((item, ok, evidence))
    print(f"[{item}] {'PASS' if ok else 'FAIL'} — {evidence}", flush=True)


def read_token() -> str:
    m = re.search(r'token\s*=\s*"([^"]+)"', TOML.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit("token not found（游戏需先带 bridge 启动一次以生成）")
    return m.group(1)


async def stats(client: BridgeClient) -> dict[str, Any]:
    return await client.call("getStats")


async def position(client: BridgeClient) -> tuple[float, float, float] | None:
    s = await stats(client)
    p = s.get("position") if isinstance(s, dict) else None
    if not p:
        return None
    return float(p["x"]), float(p["y"]), float(p["z"])


async def wait_in_game(client: BridgeClient, timeout: float = 300.0) -> None:
    t0 = time.monotonic()
    while True:
        s = await stats(client)
        if isinstance(s, dict) and s.get("in_game"):
            print(f"[boot] in_game=true（{time.monotonic() - t0:.1f}s），"
                  f"health={s.get('health')}, pos={s.get('position')}", flush=True)
            return
        if time.monotonic() - t0 > timeout:
            raise SystemExit(f"等待入世超时（{timeout:.0f}s）")
        await asyncio.sleep(2.0)


async def wait_screen_clear(client: BridgeClient, timeout: float = 60.0) -> None:
    """T0b 教训：等加载覆盖屏消失（聊天命令才打得开）。"""
    t0 = time.monotonic()
    while True:
        gui = await client.call("getGuiState")
        if isinstance(gui, dict) and not gui.get("screen_open"):
            print(f"[boot] 加载屏已消失（等待 {time.monotonic() - t0:.1f}s）", flush=True)
            return
        if time.monotonic() - t0 > timeout:
            print("[boot] 屏障等待超时，继续（walk_to 自带屏障会再等）", flush=True)
            return
        await asyncio.sleep(1.0)


async def find_log(client: BridgeClient, prims: Primitives,
                   tries: int = 4) -> tuple[int, int, int] | None:
    """64 格内找最近 TARGET_LOG；没有就向远处走几步再找（真机地形防御）。"""
    for attempt in range(tries):
        result = await client.call("world.query",
                                   {"type": "blocks", "range": 64,
                                    "filter": [TARGET_LOG]})
        blocks = result.get("blocks") if isinstance(result, dict) else []
        if blocks:
            pos = await position(client) or (0.0, 0.0, 0.0)
            near = min(blocks, key=lambda b: math.dist(
                (b["x"] + 0.5, b["y"] + 0.5, b["z"] + 0.5), pos))
            return int(near["x"]), int(near["y"]), int(near["z"])
        if attempt + 1 < tries:
            pos = await position(client) or (0.0, 64.0, 0.0)
            dx, dz = 40 * (1 if attempt % 2 == 0 else -1), 40 * (attempt // 2)
            print(f"[find] 64 格内无 {TARGET_LOG}，走到 ({pos[0] + dx:.0f}, {pos[2] + dz:.0f}) 再找",
                  flush=True)
            await prims.walk_to(pos[0] + dx, pos[2] + dz)
    return None


async def nearby_log_drops(client: BridgeClient,
                           pos: tuple[float, float, float],
                           radius: float = 12.0) -> list[dict[str, Any]]:
    """radius 内的 TARGET_LOG 掉落物实体（T7 载荷：item 注册名字段）。"""
    result = await client.call("world.query",
                               {"type": "entities", "range": radius,
                                "filter": ["minecraft:item"]})
    entities = result.get("entities") if isinstance(result, dict) else []
    return [e for e in entities
            if isinstance(e, dict) and e.get("item") == TARGET_LOG]


async def item1_payload_smoke(client: BridgeClient) -> None:
    """T7 bridge 载荷：item 实体条目带 item 注册名与 count（无掉落也验证字段通路：
    用 leaves/任意在地板上的掉落，或仅确认查询不炸 + 字段形态）。"""
    print("\n===== 验收 1：entities 载荷 item/count 字段 =====", flush=True)
    result = await client.call("world.query",
                               {"type": "entities", "range": 32,
                                "filter": ["minecraft:item"]})
    entities = result.get("entities", []) if isinstance(result, dict) else []
    with_item = [e for e in entities if isinstance(e, dict) and "item" in e]
    ok = bool(entities) and len(with_item) == len(entities) \
        and all("count" in e and isinstance(e.get("type"), str)
                and e.get("type") == "minecraft:item" for e in entities)
    sample = entities[0] if entities else {}
    record("1 entities载荷", ok,
           f"count={len(entities)} 首条字段={sorted(sample.keys()) if sample else '无掉落物'}"
           + (f" item={sample.get('item')} n={sample.get('count')}" if sample else ""))


async def item2_collect_pickup(client: BridgeClient, prims: Primitives) -> None:
    """主场景：挖 1 根 oak_log → 掉落物被捡起（实体消失 + 话术含"已捡起"）。"""
    print(f"\n===== 验收 2：collect_block(['{TARGET_LOG}'], 1) 挖后拾取 =====", flush=True)
    s = await stats(client)
    if not (isinstance(s, dict) and s.get("health", 20) >= MIN_HEALTH):
        record("2 collect拾取", False, f"血量 {s.get('health') if isinstance(s, dict) else '?'} 不足，跳过")
        return
    target = await find_log(client, prims)
    if target is None:
        record("2 collect拾取", False, f"多次走位后仍找不到 {TARGET_LOG}")
        return
    print(f"[2] 目标树干 {target}", flush=True)
    base_drops = await nearby_log_drops(client, await position(client) or (0.0, 64.0, 0.0))
    print(f"[2] 挖前基线：附近 {TARGET_LOG} 掉落 {len(base_drops)} 个", flush=True)

    t0 = time.monotonic()
    outcome = await prims.collect_block([TARGET_LOG], 1, cancel=lambda: False)
    elapsed = time.monotonic() - t0
    after_drops = await nearby_log_drops(client, await position(client) or (0.0, 64.0, 0.0))
    text_ok = ("已挖到 1/1" in outcome.text) and ("已捡起" in outcome.text)
    gone_ok = len(after_drops) <= len(base_drops)  # 基线外没新增、基线内没涨
    record("2 collect拾取", text_ok and gone_ok,
           f"{elapsed:.1f}s 话术=\"{outcome.text}\" "
           f"掉落实体 {len(base_drops)}→{len(after_drops)}")


async def item3_no_pickup(client: BridgeClient, prims: Primitives) -> None:
    """pickup=False 对照：挖后掉落物留在地上（挖通道/清理地形场景）。"""
    print(f"\n===== 验收 3：collect_block(pickup=False) 掉落保留 =====", flush=True)
    target = await find_log(client, prims)
    if target is None:
        record("3 pickup=False", False, f"找不到 {TARGET_LOG}")
        return
    base_drops = await nearby_log_drops(client, await position(client) or (0.0, 64.0, 0.0))
    outcome = await prims.collect_block([TARGET_LOG], 1, pickup=False, cancel=lambda: False)
    after_drops = await nearby_log_drops(client, await position(client) or (0.0, 64.0, 0.0))
    kept = len(after_drops) > len(base_drops)
    record("3 pickup=False", ("已挖到 1/1" in outcome.text) and kept,
           f"话术=\"{outcome.text}\" 掉落实体 {len(base_drops)}→{len(after_drops)}")


async def main() -> int:
    items = set(sys.argv[1:]) or {"1", "2"}
    client = BridgeClient(BridgeConfig(url="ws://127.0.0.1:8765", token=read_token(),
                                        request_timeout=90.0))
    await client.connect()
    prims = Primitives(client, poll_interval=0.5)
    try:
        await wait_in_game(client)
        await wait_screen_clear(client)
        if "1" in items:
            await item1_payload_smoke(client)
        if "2" in items:
            await item2_collect_pickup(client, prims)
        if "3" in items:
            await item3_no_pickup(client, prims)
    finally:
        await client.close()
    failed = [name for name, ok, _ in RESULTS if not ok]
    print("\n===== T7 真机验收汇总 =====", flush=True)
    for name, ok, evidence in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: {evidence}", flush=True)
    print(f"T7 PROBE {'PASS' if not failed else 'FAIL'}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
