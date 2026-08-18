"""命令行启动 mock bridge。

用法::

    python -m sirius_brain.mock --port 8765 --script scene.json --replay frames.jsonl
"""

import argparse
import asyncio
import json
from pathlib import Path

from . import MockBridgeServer, MockScript, load_replay


def _dump_example_script(path: Path) -> None:
    """生成一份注释齐全的示例脚本（带 --init-script 时）。"""
    example = {
        "protocol_version": "1.0",
        "tools": {
            "getStats": {"result": {"health": 20, "food": 20}, "delay_ms": 30},
            "screenshot": {"result": {"tier": "full", "image_b64": "<jpeg-base64>"},
                           "delay_ms": 120},
            "input.text": {"error": {"code": -32000, "message": "GUI 未打开"}},
        },
        "task_rules": [
            {"match": "挖矿", "status": "failed", "text": "镐子断了", "delay_ms": 3000},
            {"match": "合成", "status": "ok", "text": "合成完成", "delay_ms": 1500},
            {"status": "ok", "text": "", "delay_ms": 500},
        ],
    }
    path.write_text(json.dumps(example, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m sirius_brain.mock",
        description="Sirius mock bridge（假身体）：可脚本化 WebSocket JSON 服务",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument("--script", type=Path, help="行为脚本 JSON 文件（缺省用默认剧本）")
    parser.add_argument("--replay", type=Path, help="录制帧 JSONL 文件，启动后自动回放")
    parser.add_argument("--replay-speed", type=float, default=1.0,
                        help="回放倍速（默认 1.0，测试可调大加速）")
    parser.add_argument("--init-script", type=Path, metavar="PATH",
                        help="写出一份示例行为脚本到该路径后退出")
    args = parser.parse_args()

    if args.init_script:
        _dump_example_script(args.init_script)
        print(f"示例脚本已写入 {args.init_script}")
        return

    script = MockScript.from_json_file(args.script) if args.script else MockScript()
    replay_entries = load_replay(args.replay) if args.replay else None

    async def run() -> None:
        server = MockBridgeServer(script, host=args.host, port=args.port)
        await server.start()
        print(f"mock bridge listening on {server.url} "
              f"(protocol {script.protocol_version}, "
              f"{len(script.capabilities)} capabilities)")
        if replay_entries:
            server.start_replay(replay_entries, speed=args.replay_speed)
            print(f"replaying {len(replay_entries)} entries at {args.replay_speed}x")
        await asyncio.Event().wait()  # 常驻直至 Ctrl+C

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nmock bridge stopped")


if __name__ == "__main__":
    main()
