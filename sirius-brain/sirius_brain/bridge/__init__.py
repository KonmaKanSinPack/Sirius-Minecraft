"""Bridge 客户端（大脑侧连接身体的统一入口）。spec §8.2 / §10.1 M1-D。

对 mock 身体（``sirius_brain.mock``）与真 Bridge Mod（NeoForge）同样工作：
协议一致——"大脑不绑死身体"的第一次实战。
"""

from .client import (
    CODE_CONNECTION_LOST,
    CODE_INVALID_RESPONSE,
    CODE_NOT_CONNECTED,
    BridgeClient,
    BridgeError,
    BridgeState,
    CapabilitiesInfo,
    HelloFrame,
    HelloResult,
)
from .config import BridgeConfig

__all__ = [
    "BridgeClient",
    "BridgeConfig",
    "BridgeError",
    "BridgeState",
    "CapabilitiesInfo",
    "HelloFrame",
    "HelloResult",
    "CODE_CONNECTION_LOST",
    "CODE_NOT_CONNECTED",
    "CODE_INVALID_RESPONSE",
]
