"""Agent CLI：装配 AgentConfig + BridgeClient + QwenVLM + AgentLoop 并常驻运行。

用法::

    python -m sirius_brain.agent --local-md local.md [--url ws://127.0.0.1:8765]
                                 [--token XXX] [--max-steps 25] [-v]

启动流程：装载 local.md 的 ```env 围栏块（VLM 配置；key 只存在 gitignored 文件）→
连接 bridge（mock 或真 Mod 同一协议）→ 识别自身 uuid → 订阅 chat → 打印就绪信息
（自身 uuid / 工具表 / 步数与 token 预算）→ 常驻等待玩家聊天指令。
Ctrl+C 优雅退出（回收后台任务、关闭连接）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import replace

from sirius_brain.bridge.client import BridgeClient, BridgeError

from .config import AgentConfig
from .loop import AgentLoop
from .vlm import QwenVLM


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sirius_brain.agent",
        description="Sirius 最小整机大脑循环（M3-B）：聊天指令 → 感知 → VLM → 工具 → 游戏内回话",
    )
    parser.add_argument(
        "--local-md", default="local.md",
        help="local.md 路径（读首个 ```env 围栏块的 SIRIUS_VLM_* / SIRIUS_BRIDGE_* 配置；"
             "默认当前目录 local.md）")
    parser.add_argument("--url", help="覆盖 bridge WebSocket 地址（默认 ws://127.0.0.1:8765）")
    parser.add_argument("--token", help="覆盖 bridge hello token（真 Mod 要求）")
    parser.add_argument("--max-steps", type=int, help="单任务最大 VLM 步数（默认 25）")
    parser.add_argument("-v", "--verbose", action="store_true", help="调试日志（DEBUG 级）")
    return parser


def load_config(args: argparse.Namespace) -> AgentConfig:
    """按 CLI 参数装载 AgentConfig（显式参数覆盖 local.md env 块）。"""
    config = AgentConfig.from_local_md(args.local_md)
    if args.url or args.token:
        config.bridge = config.bridge.with_overrides(url=args.url, token=args.token)
    if args.max_steps is not None:
        config.loop = replace(config.loop, max_steps=args.max_steps)
    return config


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = load_config(args)
    client = BridgeClient(config.bridge)
    vlm = QwenVLM(config.vlm)
    agent = AgentLoop(client, vlm, config)

    agent.install()  # chat handler 先注册，再连接（最早事件也不漏）
    try:
        await client.connect()
    except BridgeError as exc:
        print(f"无法连接 bridge {config.bridge.url}：{exc}", file=sys.stderr)
        return 1

    hello = client.hello_result
    await agent.identify_self()  # 先识别自身，就绪信息里就能带上 uuid
    print("=== Sirius Agent 就绪（M3-B）===")
    print(f"Bridge: {config.bridge.url}"
          + (f"（hello={hello.status}）" if hello else ""))
    print(f"自身 uuid: {agent.self_uuid or '未知——自回显过滤以抑制窗为主（5s）'}")
    print(f"工具表（{len(agent.registry)}）: {', '.join(agent.registry.names())}")
    print(f"预算: max_steps={config.loop.max_steps}, "
          f"token={config.loop.max_total_tokens}")
    print(f"VLM 模型: {config.vlm.model}")
    print("等待玩家聊天指令（急停词：停下 / stop）。Ctrl+C 退出。")

    try:
        await agent.run()
    except asyncio.CancelledError:
        pass  # Ctrl+C：asyncio.run 取消主协程
    finally:
        await agent.shutdown()
        await client.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_argparser().parse_args(argv)
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，已退出。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
