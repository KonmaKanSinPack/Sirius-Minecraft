"""Bridge 客户端 CLI 冒烟：连接身体 → 能力协商 → 订阅事件 → （可选）等推送 → 退出。

用法::

    python -m sirius_brain.bridge --url ws://127.0.0.1:8765 [--token xxx] [--wait 2]

配置优先级：CLI 参数 > --config JSON 文件 > 环境变量（SIRIUS_BRIDGE_*）> 默认值。
连不上给清晰错误（退出码 1）；对 mock 与真 Mod（NeoForge Bridge）同样可用。
"""

import argparse
import asyncio
import json
import logging
import sys

from . import BridgeClient, BridgeConfig, BridgeError


def _build_config(args: argparse.Namespace) -> BridgeConfig:
    """CLI 参数 > --config 文件 > 环境变量 > 默认值。"""
    config = BridgeConfig.from_env()
    if args.config:
        file_config = BridgeConfig.from_json_file(args.config)
        with open(args.config, encoding="utf-8") as f:
            present = set(json.load(f))  # 只用文件里明确写的键覆盖环境变量
        config = config.with_overrides(
            **{key: getattr(file_config, key) for key in present})
    return config.with_overrides(
        url=args.url, token=args.token, request_timeout=args.timeout)


async def _smoke(config: BridgeConfig, wait: float) -> int:
    states: list[tuple[str, str]] = []

    def on_state(state, detail):
        states.append((state.value, detail))

    client = BridgeClient(config, on_state_change=on_state)
    try:
        await client.connect()
    except BridgeError as exc:
        print(f"[错误] {exc.message}", file=sys.stderr)
        print("请确认身体已启动（mock：python -m sirius_brain.mock；"
              "真 Mod：启动带 Bridge 的 NeoForge 客户端）", file=sys.stderr)
        return 1

    try:
        info = await client.capabilities()
        print(f"已连接 {config.url}")
        print(f"能力协商：protocol {info.protocol_version}，{len(info.capabilities)} 项能力")
        print("  " + "  ".join(f"{cap.name}@{cap.version}" for cap in info.capabilities))

        if config.token:
            hello = await client.wait_hello(timeout=config.hello_timeout + 1.0)
            status = hello.status if hello else "未知"
            detail = f"（{hello.detail}）" if hello and hello.detail else ""
            print(f"token 握手（best-effort）：{status}{detail}")

        result = await client.subscribe_events(
            ["chat", "health", "gui_change", "fire", "death", "weather"])
        print(f"事件订阅：{json.dumps(result, ensure_ascii=False)}")

        if wait > 0:
            @client.on_event("*")
            def _on_event(frame):
                print(f"事件推送：seq={frame.seq} event={frame.event} "
                      f"data={json.dumps(frame.data, ensure_ascii=False)}")

            print(f"等待事件推送 {wait:.1f}s（Ctrl+C 提前退出）……")
            await asyncio.sleep(wait)
    finally:
        await client.close()
    print("冒烟通过，退出")
    return 0


def main() -> None:
    # 重定向/管道输出按 UTF-8 写（Windows GBK 代码页下也能保住中文）
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        prog="python -m sirius_brain.bridge",
        description="Sirius bridge 客户端冒烟：连接身体（mock 或真 Mod），"
                    "打印能力协商结果并订阅事件",
    )
    parser.add_argument("--url", help="身体 WebSocket 地址（默认 ws://127.0.0.1:8765）")
    parser.add_argument("--token", help="token 握手凭据（真 Mod 要求；mock 忽略）")
    parser.add_argument("--timeout", type=float,
                        help="单次请求超时秒数（默认 30）")
    parser.add_argument("--config", help="BridgeConfig JSON 文件（键=字段名）")
    parser.add_argument("--wait", type=float, default=0.0,
                        help="订阅后等待事件推送的秒数（默认 0，立即退出）")
    parser.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s")

    try:
        config = _build_config(args)
    except ValueError as exc:
        print(f"[错误] 配置无效：{exc}", file=sys.stderr)
        raise SystemExit(2)

    try:
        code = asyncio.run(_smoke(config, args.wait))
    except KeyboardInterrupt:
        print("\n中断退出")
        raise SystemExit(130)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
