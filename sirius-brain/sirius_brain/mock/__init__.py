"""Mock bridge（假身体）：可脚本化 + 可回放 + 可变世界状态的 WebSocket 服务，大脑开发用。spec §10.1 M0。"""

from .fakeworld import FakeWorldBridge
from .replay import ReplayEntry, load_replay
from .script import MockScript, ScriptedTask, ScriptedToolResponse, default_capabilities
from .server import MockBridgeServer

__all__ = [
    "FakeWorldBridge",
    "MockBridgeServer",
    "MockScript",
    "ScriptedTask",
    "ScriptedToolResponse",
    "default_capabilities",
    "ReplayEntry",
    "load_replay",
]
