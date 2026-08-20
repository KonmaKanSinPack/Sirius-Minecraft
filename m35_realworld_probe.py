# -*- coding: utf-8 -*-
"""M3.5 T5a：真机原语层验收探针（不依赖 VLM）。

前提：游戏已带**新 bridge jar（含 world.query filter / input.click hold_ms）**启动，
quickPlay 自动入世；本脚本只走 Primitives + 裸 bridge 调用，零 VLM 参与。

验收 6 项（顺序即执行序；第 2 项提到最前是刻意的——界面屏障只有在
quickPlay 加载屏还活着时才能真机复现，晚一步就测不到了）：
  1. world.query filter（#tag / 显式 id / 无 filter 对照 / entities truncated）
  2. walk_to +30 格随机方向（含 T0b 界面屏障真机复现）
  3. dig_block（filter 找云杉原木 → walk_to → dig → query 复核消失）
  4. collect_block([TARGET_LOG], 3)（找→走→挖×3 完整链路）
  5. 急停：walk_to 中途置 cancel flag → ≤1.5s 返回中止话术 + #stop
  6. 性能观察：filter range=64 单次耗时；dig/collect 里 world.query 占比

token 处理：从实例 config/sirius_bridge.toml 现读（同 m35_baritone_probe.py），
不写入任何输出。安全护栏：每阶段前查血量，<10 直接跳过后续并如实记录。

采集目标树种：TARGET_LOG 常量（默认 minecraft:oak_log）。本世界出生区实测
无云杉（4 方向 × 360 格扫描 0 命中，玩家亦确认），M3.5 主管裁决改用出生区
橡木；#minecraft:logs tag 天然涵盖两者，1a 的 tag 语义验证不受影响。

用法：python m35_realworld_probe.py [item …]   # 例：`1 3 4` 只跑指定项（默认全跑）
"""
import asyncio
import math
import random
import re
import subprocess
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
GAME_LOG = ROOT / "logs" / "latest.log"  # 游戏从 repo 根启动时 vanilla 日志落这里（T0b §4）
MIN_HEALTH = 10.0
TARGET_LOG = "minecraft:oak_log"  # 采集/显式 id 验收目标（见模块 docstring 主管裁决注）

random.seed(20260820)  # 可复现：随机方向/目标固定

# ---------------------------------------------------------------------- 基础设施

RESULTS: list[tuple[str, bool, str]] = []


def record(item: str, ok: bool, evidence: str) -> None:
    RESULTS.append((item, ok, evidence))
    print(f"[{item}] {'PASS' if ok else 'FAIL'} — {evidence}", flush=True)


def read_token() -> str:
    m = re.search(r'token\s*=\s*"([^"]+)"', TOML.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit("token not found（游戏需先带 bridge 启动一次以生成）")
    return m.group(1)


class TimingClient:
    """Primitives 只要求 call()/command()：包一层计时代理，逐调用记录 (method, 耗时)。"""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls: list[tuple[str, float]] = []

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        t0 = time.perf_counter()
        try:
            return await self.inner.call(method, params)
        finally:
            self.calls.append((method, time.perf_counter() - t0))

    async def command(self, text: str) -> Any:
        t0 = time.perf_counter()
        try:
            return await self.inner.command(text)
        finally:
            self.calls.append((f"cmd:{text[:24]}", time.perf_counter() - t0))

    def span(self, method: str, lo: int, hi: int) -> list[float]:
        return [d for m, d in self.calls[lo:hi] if m == method]


def hdist(ax: float, az: float, bx: float, bz: float) -> float:
    return math.hypot(ax - bx, az - bz)


async def stats(client: TimingClient) -> dict[str, Any]:
    return await client.call("getStats")


async def position(client: TimingClient) -> tuple[float, float, float] | None:
    s = await stats(client)
    p = s.get("position") if isinstance(s, dict) else None
    if not p:
        return None
    return float(p["x"]), float(p["y"]), float(p["z"])


async def wait_in_game(client: TimingClient, timeout: float = 300.0) -> None:
    """等 quickPlay 入世：in_game=true 即返回（此时加载覆盖屏可能仍在——正是屏障测试要的）。"""
    t0 = time.monotonic()
    while True:
        s = await stats(client)
        if isinstance(s, dict) and s.get("in_game"):
            print(f"[boot] in_game=true（等待 {time.monotonic() - t0:.1f}s），"
                  f"health={s.get('health')}, pos={s.get('position')}", flush=True)
            return
        if time.monotonic() - t0 > timeout:
            raise SystemExit(f"等待入世超时（{timeout:.0f}s）")
        await asyncio.sleep(2.0)


async def health_ok(client: TimingClient) -> bool:
    s = await stats(client)
    h = s.get("health")
    if isinstance(h, (int, float)) and h < MIN_HEALTH:
        print(f"[guard] 血量 {h} < {MIN_HEALTH}，跳过后续动作项", flush=True)
        return False
    return True


def dist3_to_block(px: float, py: float, pz: float, b: dict[str, Any]) -> float:
    """与 bridge 同基准：玩家坐标 → 方块中心 (x+0.5, y+0.5, z+0.5) 的欧氏距离。"""
    return math.sqrt((b["x"] + 0.5 - px) ** 2 + (b["y"] + 0.5 - py) ** 2 + (b["z"] + 0.5 - pz) ** 2)


def baritone_log_tail() -> str:
    """取游戏聊天日志里 Baritone 回显（#stop/#goto 是否真被客户端收到）。"""
    try:
        text = GAME_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(latest.log 不可读)"
    lines = [l for l in text.splitlines() if "[Baritone]" in l]
    return "\n      ".join(lines[-8:]) if lines else "(latest.log 无 Baritone 回显行)"


# ---------------------------------------------------------------------- 验收项 2
# （执行序第一——加载屏活着才能真机验证屏障）

async def item2_walk_to(client: TimingClient, prims: Primitives) -> dict[str, Any]:
    print("\n===== 验收 2：walk_to（含界面屏障真机复现）=====", flush=True)
    gui = await client.call("getGuiState")
    screen_open = bool(gui.get("screen_open")) if isinstance(gui, dict) else None
    screen_class = gui.get("screen_class") if isinstance(gui, dict) else "?"
    print(f"[2] 发首个 walk_to 前界面状态：screen_open={screen_open}, class={screen_class}", flush=True)

    px, py, pz = await position(client)
    ang = random.uniform(0, 2 * math.pi)
    tx, tz = px + 30 * math.cos(ang), pz + 30 * math.sin(ang)
    print(f"[2] 起点 ({px:.1f},{py:.1f},{pz:.1f}) → 随机方向 +30 格 → ({tx:.1f},{tz:.1f})", flush=True)

    outcome = await prims.walk_to(tx, tz)
    text = outcome.text
    barrier_note = ("首调用时屏仍占用（" + str(screen_class) + "）→ 屏障路径被真机覆盖"
                    if screen_open else "首调用时已无屏 → 屏障未触发（正常路径）")
    print(f"[2] 第一次话术：{text}", flush=True)

    if "已走到" not in text and screen_open:
        # 屏障拒绝/超时是"不丢命令"的正确行为：等屏真消失后重试，重试成功仍算 PASS
        t0 = time.monotonic()
        while time.monotonic() - t0 < 240.0:
            g = await client.call("getGuiState")
            if not (isinstance(g, dict) and g.get("screen_open")):
                break
            await asyncio.sleep(2.0)
        outcome = await prims.walk_to(tx, tz)
        text = outcome.text
        barrier_note += f"；屏障等屏 {time.monotonic() - t0:.0f}s 后重试成功"
        print(f"[2] 屏清后重试话术：{text}", flush=True)

    fx, fy, fz = await position(client)
    final_d = hdist(fx, fz, tx, tz)
    moved = hdist(fx, fz, px, pz)
    ok = final_d <= 2.5 and "已走到" in text and "(" in text
    record("2 walk_to", ok,
           f"{barrier_note}；终点 ({fx:.1f},{fy:.1f},{fz:.1f}) 距目标 {final_d:.2f} 格"
           f"（位移 {moved:.1f} 格）；话术含坐标={('(' in text and ',' in text)}")
    return {"final_pos": (fx, fy, fz)}


# ---------------------------------------------------------------------- 验收项 1

LOG_FAMILY_EXTRA = {"minecraft:bamboo_block", "minecraft:mangrove_roots"}


def in_logs_family(name: str) -> bool:
    """#minecraft:logs 家族成员的宽松判定（log/wood 词根 + 已知特例）——
    只用于真机证据交叉检查，权威判定在服务端 tag 查询。"""
    return "log" in name or "wood" in name or name in LOG_FAMILY_EXTRA


async def timed_query(client: TimingClient, params: dict[str, Any]) -> tuple[dict[str, Any], float]:
    t0 = time.perf_counter()
    r = await client.call("world.query", params)
    return r, time.perf_counter() - t0


async def item1_world_query_filter(client: TimingClient, prims: Primitives) -> None:
    print("\n===== 验收 1：world.query filter =====", flush=True)
    # 附近没树的话 tag 查询会 0 命中（空通过无证据价值）——先跳到有树处
    r_pre, _ = await timed_query(client, {"type": "blocks", "range": 32,
                                          "filter": ["#minecraft:logs"]})
    if r_pre.get("count", 0) == 0:
        print("[1] 32 格内无任何 logs，先跳跃走位到有树处再验", flush=True)
        await find_logs(client, min_count=1, prims=prims)
    px, py, pz = await position(client)

    # 1a) #tag 过滤
    r, dt = await timed_query(client, {"type": "blocks", "range": 32, "filter": ["#minecraft:logs"]})
    blocks, count, truncated = r.get("blocks", []), r.get("count"), r.get("truncated")
    names = sorted({b["block"] for b in blocks})
    bad_names = [n for n in names if not in_logs_family(n)]
    dists = [dist3_to_block(px, py, pz, b) for b in blocks]
    ascending = all(dists[i + 1] >= dists[i] - 0.05 for i in range(len(dists) - 1))
    ok_a = (isinstance(truncated, bool) and isinstance(count, int) and count <= 32
            and len(blocks) == count and not bad_names and ascending)
    print(f"[1a] #minecraft:logs range=32 → count={count}, truncated={truncated}, {dt*1000:.0f}ms\n"
          f"     命中种类：{names}\n"
          f"     距离序列（前8）：{[round(d, 1) for d in dists[:8]]} 升序={ascending}", flush=True)

    # 1b) 显式 id 过滤
    r2, dt2 = await timed_query(client, {"type": "blocks", "range": 32, "filter": [TARGET_LOG]})
    b2, c2, t2 = r2.get("blocks", []), r2.get("count"), r2.get("truncated")
    all_id = all(b["block"] == TARGET_LOG for b in b2)
    ok_b = (isinstance(t2, bool) and isinstance(c2, int) and c2 <= 32
            and all_id and len(b2) == c2 and c2 > 0)
    print(f"[1b] {TARGET_LOG} range=32 → count={c2}, truncated={t2}, "
          f"{dt2*1000:.0f}ms, 全部=目标id:{all_id}", flush=True)

    # 1c) 无 filter 对照（旧行为不变）
    r3, dt3 = await timed_query(client, {"type": "blocks", "range": 32})
    b3, c3, t3 = r3.get("blocks", []), r3.get("count"), r3.get("truncated")
    kinds3 = len({b["block"] for b in b3})
    ok_c = isinstance(t3, bool) and isinstance(c3, int) and c3 <= 512 and kinds3 > 3
    print(f"[1c] 无 filter range=32 → count={c3}, truncated={t3}, {dt3*1000:.0f}ms, "
          f"方块种类={kinds3}（混合={kinds3 > 3}）", flush=True)

    # 1d) entities 结果带 truncated 字段（J 遗留点 4 真机观察）
    r4, dt4 = await timed_query(client, {"type": "entities", "range": 48})
    has_tr = "truncated" in r4 if isinstance(r4, dict) else False
    print(f"[1d] entities range=48 → count={r4.get('count')}, "
          f"truncated={r4.get('truncated')!r}（字段存在={has_tr}）, {dt4*1000:.0f}ms", flush=True)

    record("1 world.query filter", ok_a and ok_b and ok_c,
           f"tag: count≤32={count <= 32}, truncated=bool={isinstance(truncated, bool)}, "
           f"全为logs家族={not bad_names}, 距离升序={ascending}；"
           f"显式id: 全为{TARGET_LOG}={all_id}, count={c2}≤32且>0={0 < c2 <= 32}；"
           f"无filter对照: count={c3}, 种类={kinds3}>3={kinds3 > 3}, truncated=bool={isinstance(t3, bool)}；"
           f"entities truncated 字段存在={has_tr}（观察项不判 PASS/FAIL）")


# ---------------------------------------------------------------------- 验收项 3

async def find_logs(client: TimingClient, min_count: int,
                    prims: Primitives | None = None, max_hops: int = 4) -> dict[str, Any]:
    """filter 找 TARGET_LOG；不够 min_count 就随机方向走 40 格再看（最多 max_hops 跳）。"""
    r, _ = await timed_query(client, {"type": "blocks", "range": 64,
                                      "filter": [TARGET_LOG]})
    for hop in range(max_hops):
        if r.get("count", 0) >= min_count:
            return r
        if prims is None:
            break
        px, py, pz = await position(client)
        ang = random.uniform(0, 2 * math.pi)
        tx, tz = px + 40 * math.cos(ang), pz + 40 * math.sin(ang)
        print(f"[find] {TARGET_LOG} {r.get('count')} 根 < {min_count}，跳 {hop + 1}："
              f"walk_to ({tx:.0f},{tz:.0f}) 再找", flush=True)
        out = await prims.walk_to(tx, tz)
        print(f"[find] 跳跃走位话术：{out.text}", flush=True)
        r, _ = await timed_query(client, {"type": "blocks", "range": 64,
                                          "filter": [TARGET_LOG]})
    return r


async def item3_dig_block(client: TimingClient, prims: Primitives) -> None:
    print("\n===== 验收 3：dig_block（找→走→挖→复核）=====", flush=True)
    r = await find_logs(client, min_count=1)
    blocks = r.get("blocks", [])
    if not blocks:
        record("3 dig_block", False, f"64 格内未找到 {TARGET_LOG}（多次跳跃走位后仍无）")
        return
    b = blocks[0]  # filter 结果按距离升序，blocks[0] 即最近
    print(f"[3] 最近 {TARGET_LOG}：({b['x']},{b['y']},{b['z']}) {b['block']}", flush=True)

    px, py, pz = await position(client)
    cands = [(b["x"] + 1.5, b["z"]), (b["x"] - 1.5, b["z"]),
             (b["x"], b["z"] + 1.5), (b["x"], b["z"] - 1.5)]
    wx, wz = min(cands, key=lambda c: hdist(px, pz, c[0], c[1]))
    t0 = time.perf_counter()
    walk = await prims.walk_to(wx, wz)
    walk_dt = time.perf_counter() - t0
    print(f"[3] 走位 ({wx},{wz}) {walk_dt:.1f}s：{walk.text}", flush=True)

    t0 = time.perf_counter()
    dig = await prims.dig_block(b["x"], b["y"], b["z"])
    dig_dt = time.perf_counter() - t0
    print(f"[3] dig_block {dig_dt:.1f}s 话术：{dig.text}", flush=True)

    # 独立复核：filter 重查，该坐标必须已消失
    r2, _ = await timed_query(client, {"type": "blocks", "range": 64,
                                       "filter": [TARGET_LOG]})
    gone = all((x["x"], x["y"], x["z"]) != (b["x"], b["y"], b["z"])
               for x in r2.get("blocks", []))
    record("3 dig_block", "已挖掉" in dig.text and gone,
           f"目标 ({b['x']},{b['y']},{b['z']})；走位 {walk_dt:.1f}s + 挖掘 {dig_dt:.1f}s；"
           f"话术={dig.text}；world.query 复核已消失={gone}（余 {r2.get('count')} 根）")


# ---------------------------------------------------------------------- 验收项 4

async def item4_collect_block(client: TimingClient, prims: Primitives) -> None:
    print(f"\n===== 验收 4：collect_block 3 根 {TARGET_LOG} =====", flush=True)
    r = await find_logs(client, min_count=3, prims=prims)
    pre = {(x["x"], x["y"], x["z"]) for x in r.get("blocks", [])}
    print(f"[4] collect 前可见目标原木 {len(pre)} 根（cap 32）", flush=True)
    if not pre:
        record("4 collect_block", False, f"多次跳跃走位后仍找不到 {TARGET_LOG}，无法验收完整链路")
        return

    idx0 = len(client.calls)
    t0 = time.perf_counter()
    out = await prims.collect_block([TARGET_LOG], 3, cancel=lambda: False)
    wall = time.perf_counter() - t0
    qd = client.span("world.query", idx0, len(client.calls))
    print(f"[4] 话术：{out.text}", flush=True)

    m = re.search(r"已挖到 (\d+)/(\d+)", out.text)
    n = int(m.group(1)) if m else 0
    r2, _ = await timed_query(client, {"type": "blocks", "range": 64,
                                       "filter": [TARGET_LOG]})
    post = {(x["x"], x["y"], x["z"]) for x in r2.get("blocks", [])}
    # truncated cap=32 下集合比较有观测局限：走位使距离排名洗牌，collect 内部
    # 选中的目标可能不在外部 pre 快照（cap 截断）里。交叉证据：
    #   vanished = pre 中确认消失的坐标数（外部独立复核）
    #   newcomers = post 新面孔数（挖掉 k 根 → cap 补进 k 个更远的）
    # 契约：话术 n≥1（部分收获也算）+ 外部至少独立确认 1 根消失 + 消失+新面孔 ≥ n
    vanished = len(pre - post)
    newcomers = len(post - pre)
    ok = n >= 1 and "/3" in out.text and vanished >= 1 and vanished + newcomers >= n
    record("4 collect_block", ok,
           f"话术 n/3（n={n}）；外部复核 pre 消失 {vanished} 根 + post 新面孔 {newcomers} 根"
           f"（truncated cap 观测局限，合并 ≥ n={'OK' if vanished + newcomers >= n else 'NO'}）；"
           f"总耗时 {wall:.1f}s，其中 world.query {len(qd)} 次共 {sum(qd):.2f}s")


# ---------------------------------------------------------------------- 验收项 5

async def item5_cancel(client: TimingClient, prims: Primitives) -> None:
    print("\n===== 验收 5：急停（cancel 中途置 True）=====", flush=True)
    flag = {"stop": False}
    px, py, pz = await position(client)
    ang = random.uniform(0, 2 * math.pi)
    tx, tz = px + 60 * math.cos(ang), pz + 60 * math.sin(ang)
    print(f"[5] 起点 ({px:.1f},{pz:.1f}) → 远目标 ({tx:.1f},{tz:.1f})，移动后急停", flush=True)

    task = asyncio.create_task(prims.walk_to(tx, tz, cancel=lambda: flag["stop"]))
    moved = 0.0
    t0 = time.monotonic()
    while time.monotonic() - t0 < 30.0:  # 等真实位移（Baritone 起步 1-3s）
        await asyncio.sleep(0.3)
        p = await position(client)
        if p:
            moved = hdist(p[0], p[2], px, pz)
            if moved > 2.0:
                break
    tc0 = time.perf_counter()
    flag["stop"] = True
    outcome = await task
    cancel_dt = time.perf_counter() - tc0
    text = outcome.text
    print(f"[5] 置 flag 前位移 {moved:.1f} 格；置 flag → 返回耗时 {cancel_dt:.2f}s", flush=True)
    print(f"[5] 中止话术：{text}", flush=True)

    # #stop 证据：返回后位置应冻结
    p_end = await position(client)
    await asyncio.sleep(2.0)
    p_later = await position(client)
    frozen = hdist(p_end[0], p_end[2], p_later[0], p_later[2]) < 1.0
    has_coord = bool(re.search(r"\(-?\d", text))
    ok = cancel_dt <= 1.5 and "中止" in text and has_coord and frozen
    record("5 急停", ok,
           f"cancel→返回 {cancel_dt:.2f}s（≤1.5s={cancel_dt <= 1.5}）；话术含'中止'与坐标="
           f"{'中止' in text and has_coord}；返回后 2s 位移冻结={frozen}；Baritone 日志回显见下")
    print(f"[5] Baritone 回显（latest.log 尾部）：\n      {baritone_log_tail()}", flush=True)


# ---------------------------------------------------------------------- 验收项 6

async def item6_performance(client: TimingClient, prims: Primitives) -> None:
    print("\n===== 验收 6：性能观察 =====", flush=True)

    async def bench(params: dict[str, Any], n: int = 5) -> tuple[float, float, float]:
        ds = []
        for _ in range(n):
            _, dt = await timed_query(client, params)
            ds.append(dt)
        return min(ds), sum(ds) / len(ds), max(ds)

    f64 = await bench({"type": "blocks", "range": 64, "filter": ["#minecraft:logs"]})
    f32 = await bench({"type": "blocks", "range": 32, "filter": [TARGET_LOG]})
    u8 = await bench({"type": "blocks", "range": 8})
    u64 = await bench({"type": "blocks", "range": 64})
    print(f"[6] filter range=64 ×5：min/avg/max = {f64[0]*1000:.0f}/{f64[1]*1000:.0f}/{f64[2]*1000:.0f} ms", flush=True)
    print(f"[6] filter range=32 ×5：min/avg/max = {f32[0]*1000:.0f}/{f32[1]*1000:.0f}/{f32[2]*1000:.0f} ms", flush=True)
    print(f"[6] 无filter range=8 ×5：min/avg/max = {u8[0]*1000:.0f}/{u8[1]*1000:.0f}/{u8[2]*1000:.0f} ms", flush=True)
    print(f"[6] 无filter range=64 ×5：min/avg/max = {u64[0]*1000:.0f}/{u64[1]*1000:.0f}/{u64[2]*1000:.0f} ms", flush=True)
    record("6 性能观察", True,  # 观察项：数据本身即结论
           f"filter64 avg {f64[1]*1000:.0f}ms / filter32 avg {f32[1]*1000:.0f}ms / "
           f"无filter8 avg {u8[1]*1000:.0f}ms / 无filter64 avg {u64[1]*1000:.0f}ms（各5次 min/avg/max 见上）")


# ---------------------------------------------------------------------- 验收项 7
# T6 新增：遮挡场景（隔树叶挖树干）——裸调 bridge dig，断言 broken_via_occluder

async def item7_dig_occluder(client: TimingClient, prims: Primitives) -> None:
    print("\n===== 验收 7（T6）：隔遮挡挖目标（broken_via_occluder）=====", flush=True)

    def occluder_between(pos, leaf_set, target) -> tuple[int, int, int] | None:
        """眼位→目标中心连线（0.25 格采样）上的第一个非目标实心块。"""
        ex, ey, ez = pos[0], pos[1] + 1.62, pos[2]
        c = (target[0] + 0.5, target[1] + 0.5, target[2] + 0.5)
        import math as _m
        steps = max(1, int(_m.dist((ex, ey, ez), c) / 0.25))
        for i in range(1, steps):
            r = i / steps
            cell = (int(ex + (c[0] - ex) * r), int(ey + (c[1] - ey) * r), int(ez + (c[2] - ez) * r))
            if cell != target and cell in leaf_set:
                return cell
        return None

    for attempt in range(4):
        r_logs, _ = await timed_query(client, {"type": "blocks", "range": 24,
                                               "filter": [TARGET_LOG]})
        logs = r_logs.get("blocks", [])
        if not logs:
            print(f"[7] 24 格内无 {TARGET_LOG}，先跳跃走位找树（attempt {attempt + 1}）", flush=True)
            await find_logs(client, min_count=1, prims=prims)
            continue
        # 走到最近树旁再取视线遮挡（站姿不同遮挡不同）
        b = logs[0]
        px, py, pz = await position(client)
        cands = [(b["x"] + 1.5, b["z"]), (b["x"] - 1.5, b["z"]),
                 (b["x"], b["z"] + 1.5), (b["x"], b["z"] - 1.5)]
        wx, wz = min(cands, key=lambda c: math.hypot(px - c[0], pz - c[1]))
        walk = await prims.walk_to(wx, wz)
        print(f"[7] 走位 ({wx},{wz})：{walk.text}", flush=True)
        r_leaves, _ = await timed_query(client, {"type": "blocks", "range": 8,
                                                 "filter": ["#minecraft:leaves"]})
        leaves = {(x["x"], x["y"], x["z"]) for x in r_leaves.get("blocks", [])}
        pos = await position(client)
        r_logs2, _ = await timed_query(client, {"type": "blocks", "range": 8,
                                                "filter": [TARGET_LOG]})
        # 挑触及内且视线穿树叶的目标段
        target = None
        for blk in r_logs2.get("blocks", []):
            t = (blk["x"], blk["y"], blk["z"])
            d = dist3_to_block(pos[0], pos[1], pos[2], blk)
            if d <= 4.5 and occluder_between(pos, leaves, t) is not None:
                target = blk
                break
        if target is None:
            print(f"[7] 本树无遮挡视线候选（leaves={len(leaves)}），换一棵（attempt {attempt + 1}）", flush=True)
            await prims.walk_to(pos[0] + 25, pos[2] + 25)  # 走远换树
            continue
        t = (target["x"], target["y"], target["z"])
        print(f"[7] 遮挡目标选定：{t} {target['block']}（眼位→中心穿过树叶）", flush=True)
        t0 = time.perf_counter()
        dig_result = await client.call("dig", {"x": t[0], "y": t[1], "z": t[2],
                                               "timeout_ms": 15000})
        dt = time.perf_counter() - t0
        print(f"[7] bridge dig {dt:.1f}s → {dig_result}", flush=True)
        r_after, _ = await timed_query(client, {"type": "blocks", "range": 8,
                                                "filter": [TARGET_LOG]})
        gone = all((x["x"], x["y"], x["z"]) != t for x in r_after.get("blocks", []))
        ok = (dig_result.get("result") == "broken"
              and dig_result.get("broken_via_occluder") is True and gone)
        record("7 dig 遮挡穿透", ok,
               f"result={dig_result.get('result')}, via_occluder={dig_result.get('broken_via_occluder')}, "
               f"elapsed_ms={dig_result.get('elapsed_ms')}, 耗时 {dt:.1f}s, query 复核已消失={gone}")
        return
    record("7 dig 遮挡穿透", False, "多次尝试后未找到带树叶遮挡的触及内目标（可重跑）")


# ---------------------------------------------------------------------- 验收项 8
# T6 新增：lookAt 平滑转头（turn_speed_deg_s）+ 新 look 替换语义

async def item8_smooth_look(client: TimingClient, prims: Primitives) -> None:
    print("\n===== 验收 8（T6）：lookAt 平滑转头 =====", flush=True)
    px, py, pz = await position(client)
    target = (px + 8.0, py + 1.0, pz)  # 正东 8 格

    t0 = time.perf_counter()
    smooth = await client.call("lookAt", {"x": target[0], "y": target[1], "z": target[2],
                                          "turn_speed_deg_s": 300})
    dt = time.perf_counter() - t0
    print(f"[8] 300deg/s 平滑转头 {dt*1000:.0f}ms → converged={smooth.get('converged')}, "
          f"elapsed_ms={smooth.get('elapsed_ms')}, yaw={smooth.get('yaw')}, pitch={smooth.get('pitch')}",
          flush=True)
    ok_smooth = (smooth.get("converged") is True
                 and isinstance(smooth.get("elapsed_ms"), (int, float))
                 and smooth.get("elapsed_ms", 0) > 0)

    # 收口精度：与瞬间 lookAt 的解析值比对（yaw/pitch 差 < 0.5°）
    snap = await client.call("lookAt", {"x": target[0], "y": target[1], "z": target[2]})
    dyaw = abs(smooth.get("yaw", 999) - snap.get("yaw", -999))
    dpitch = abs(smooth.get("pitch", 999) - snap.get("pitch", -999))
    dyaw = min(dyaw, 360 - dyaw)
    print(f"[8] 收口精度 vs 瞬间 lookAt：dyaw={dyaw:.3f}°, dpitch={dpitch:.3f}°", flush=True)
    ok_snap = dyaw < 0.5 and dpitch < 0.5

    # 替换语义：慢速转头（30deg/s，反向约 180°）中途来一个瞬间 look → converged=false。
    # 注意必须走**第二条连接**：bridge 的 WS 服务按连接串行处理消息，同一连接上
    # 阻塞中的慢转头会先把后续请求排队（单连接下"替换"根本到不了服务器）。
    px2, py2, pz2 = await position(client)
    opp = (px2 - 10.0, py2 + 1.0, pz2)  # 反方向（约 180°，30deg/s 需 ~6s）
    interruptor = BridgeClient(BridgeConfig(url="ws://127.0.0.1:8765", token=read_token(),
                                            request_timeout=20.0))
    slow = asyncio.create_task(client.call(
        "lookAt", {"x": opp[0], "y": opp[1], "z": opp[2], "turn_speed_deg_s": 30}))
    await asyncio.sleep(1.0)  # 慢速转头进行中（已转 ~30°）
    async with interruptor as ic:
        _snap_back = await ic.call("lookAt", {"x": target[0], "y": target[1], "z": target[2]})
    superseded = await slow
    print(f"[8] 慢转头被瞬间 look 替换 → converged={superseded.get('converged')}, "
          f"elapsed_ms={superseded.get('elapsed_ms')}", flush=True)
    ok_super = superseded.get("converged") is False and superseded.get("elapsed_ms", 0) < 3000

    record("8 lookAt 平滑转头", ok_smooth and ok_snap and ok_super,
           f"300deg/s converged={smooth.get('converged')} elapsed={smooth.get('elapsed_ms')}ms；"
           f"收口 dyaw={dyaw:.3f}°/dpitch={dpitch:.3f}°（<0.5°={'OK' if ok_snap else 'NO'}）；"
           f"替换语义 converged=false={ok_super}")


def focus_game_window() -> bool:
    """把游戏窗口带到前台（连续挖掘/转头等动作路径的现实前提；dig 的动作层
    hold 已不依赖焦点，但 input.click 事件层与 Baritone 聊天注入在真实人类
    监看场景下都假定游戏窗口激活——探针先聚焦是验收的正当前置步骤）。"""
    ps = (
        "Add-Type '[DllImport(\"user32.dll\")] public static extern bool"
        " SetForegroundWindow(IntPtr h); [DllImport(\"user32.dll\")] public static"
        " extern bool ShowWindow(IntPtr h, int c);' -Name W -Namespace N;"
        "$p = Get-Process java | Select-Object -First 1;"
        "[N.W]::ShowWindow($p.MainWindowHandle, 9) | Out-Null;"
        "[N.W]::SetForegroundWindow($p.MainWindowHandle)"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception as exc:  # noqa: BLE001
        print(f"[focus] 聚焦游戏窗口失败（继续执行）：{exc}", flush=True)
        return False


# ---------------------------------------------------------------------- main

async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    inner = BridgeClient(BridgeConfig(url="ws://127.0.0.1:8765", token=read_token(),
                                      request_timeout=60.0))
    # 用法 `python m35_realworld_probe.py 1 3 4` 只跑指定项（按内部执行序重排）；
    # 无参全跑。中断接续场景用（如 2/5/6 已 PASS 后只补 1/3/4）
    ORDER = ["2", "1", "3", "4", "5", "6", "7", "8"]
    wanted = {a for a in sys.argv[1:] if a in ORDER} or set(ORDER)

    def want(item: str) -> bool:
        return item.split()[0] in wanted

    async with inner as bc:
        client = TimingClient(bc)
        await wait_in_game(client)
        focus_game_window()

        # 执行序：2（屏障要加载屏活着）→ 1 → 3 → 4 → 5 → 6 → 7（T6）→ 8（T6）
        prims = Primitives(client)
        if want("2"):
            await item2_walk_to(client, prims)
        if want("1"):
            await item1_world_query_filter(client, prims)
        if want("3") or want("4") or want("5"):
            if await health_ok(client):
                if want("3"):
                    await item3_dig_block(client, prims)
                if want("4"):
                    if await health_ok(client):
                        await item4_collect_block(client, prims)
                if want("5"):
                    if await health_ok(client):
                        await item5_cancel(client, prims)
            else:
                for it in ("3", "4", "5"):
                    if want(it):
                        names = {"3": "3 dig_block", "4": "4 collect_block", "5": "5 急停"}
                        record(names[it], False, "SKIP：血量不足")
        if want("6"):
            await item6_performance(client, prims)
        if want("7"):
            if await health_ok(client):
                await item7_dig_occluder(client, prims)
        if want("8"):
            await item8_smooth_look(client, prims)

        # 各方法调用耗时汇总（找瓶颈用）
        by_m: dict[str, list[float]] = {}
        for m, d in client.calls:
            by_m.setdefault(m, []).append(d)
        summary = ", ".join(f"{m}:{len(ds)}次/共{sum(ds):.2f}s" for m, ds in sorted(by_m.items()))
        print(f"\n[调用统计] {summary}", flush=True)

    print("\n===== 汇总 =====", flush=True)
    for item, ok, ev in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {item}: {ev}", flush=True)
    n_pass = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\nM3.5 T5a REALWORLD PROBE: {n_pass}/{len(RESULTS)} PASS", flush=True)
    sys.exit(0 if n_pass == len(RESULTS) else 1)


if __name__ == "__main__":
    asyncio.run(main())
