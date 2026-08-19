"""Agent 包：M3 大脑侧的 VLM 客户端与配置。spec §10.1 M3-A。

- ``QwenVLM``：OpenAI 兼容 chat/completions + 原生 tool-calling（DashScope
  qwen3.7-plus 配方），transport 可注入（测试零网络）
- ``AgentConfig``：VLM + bridge + 循环（预留）聚合配置；local.md ```env 围栏块
  或环境变量装载，key 只存在于 gitignored 文件，绝不入库
- 工具循环本体（感知→VLM→工具执行）是 M3-B 的交付物，不在本包内
"""

from .config import (
    LoopConfig,
    VLMConfig,
    AgentConfig,
    VLM_ENV_PREFIX,
    parse_env_fenced_block,
)
from .vlm import (
    CODE_INVALID_RESPONSE,
    CODE_NETWORK_ERROR,
    QwenVLM,
    ToolCall,
    VLMError,
    VLMResponse,
    VLMUsage,
    VLMAuthError,
    sniff_image_mime,
    system_message,
    to_data_url,
    tool_result_message,
    urllib_transport,
    user_message,
)

__all__ = [
    "AgentConfig",
    "LoopConfig",
    "QwenVLM",
    "ToolCall",
    "VLMConfig",
    "VLMError",
    "VLMResponse",
    "VLMUsage",
    "VLMAuthError",
    "VLM_ENV_PREFIX",
    "CODE_NETWORK_ERROR",
    "CODE_INVALID_RESPONSE",
    "parse_env_fenced_block",
    "sniff_image_mime",
    "system_message",
    "to_data_url",
    "tool_result_message",
    "urllib_transport",
    "user_message",
]
