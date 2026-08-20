# -*- coding: utf-8 -*-
"""M3.5 T0b：Baritone 前置验证探针。

前提：游戏已启动（bridge + baritone 均在 mods），且已进入一个世界。
流程：getStats 取当前位置 → 发送 ``#goto x+20 z``（Baritone GoalXZ，客户端
拦截不达服务器）→ 轮询位置 45s，观察水平位移与到目标的收敛 → ``#stop`` 收尾。
判定：开始移动（位移 >5 格）且终点距目标 ≤3 格 → PASS。
"""
import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "sirius-brain"))

from sirius_brain.bridge.client import BridgeClient  # noqa: E402
from sirius_brain.bridge.config import BridgeConfig  # noqa: E402

TOML = ROOT / ".minecraft/versions/1.21.1-Sirius/config/sirius_bridge.toml"


def read_token() -> str:
    m = re.search(r'token\s*=\s*"([^"]+)"', TOML.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit("token not found（游戏需先带 bridge 启动一次以生成）")
    return m.group(1)


def hdist(a: dict, b: tuple[float, float]) -> float:
    return ((a["x"] - b[0]) ** 2 + (a["z"] - b[1]) ** 2) ** 0.5


async def main() -> None:
    client = BridgeClient(BridgeConfig(url="ws://127.0.0.1:8765", token=read_token()))
    async with client:
        stats = await client.call("getStats")
        assert stats.get("in_game") is True, "未检测到已进入世界"
        pos = stats["position"]
        start = (pos["x"], pos["z"])
        target = (round(pos["x"]) + 20, round(pos["z"]))
        print(f"[1] 起点 {start}（y={pos['y']:.1f}），目标列 {target}（#goto GoalXZ）")

        await client.command(f"#goto {target[0]} {target[1]}")
        print(f"[2] 已发送 #goto {target[0]} {target[1]}")

        moved_max = 0.0
        final_dist = hdist(pos, target)
        for i in range(45):
            await asyncio.sleep(1.0)
            pos = (await client.call("getStats"))["position"]
            d_now, d_tgt = hdist(pos, start), hdist(pos, target)
            moved_max = max(moved_max, d_now)
            final_dist = d_tgt
            if i % 5 == 0 or d_tgt <= 3:
                print(f"    t={i+1}s pos=({pos['x']:.1f},{pos['y']:.1f},{pos['z']:.1f}) "
                      f"位移={d_now:.1f} 距目标={d_tgt:.1f}")
            if d_tgt <= 3:
                break

        await client.command("#stop")
        print(f"[3] 已发送 #stop；最大位移={moved_max:.1f} 格，终点距目标={final_dist:.1f} 格")

    ok = moved_max > 5 and final_dist <= 3
    print("\nBARITONE SMOKE " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
