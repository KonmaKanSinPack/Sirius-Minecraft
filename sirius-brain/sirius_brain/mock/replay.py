"""录制帧回放：JSONL 每行一帧 notification，按序（delay_ms）或按时间戳（timestamp）推送。

spec §10.1 M0「回放录制的真实协议帧」。回放时 seq 重新单调编号（录制文件里的旧 seq 被忽略），
事件分级 level 亦可在录制行中标注，推送时注入 data["level"]。

JSONL 行格式（两种均可）：
- 简化行： {"event": "health", "data": {"health": 6}, "level": "CRITICAL", "delay_ms": 500}
- 整帧行： {"type": "notification", "event": "chat", "data": {}, "timestamp": 1.0, "seq": 7}
  （type/timestamp/seq 之外的载荷被采用；seq 重编号，timestamp 用作回放时间轴）
"""

import json
from pathlib import Path

from pydantic import BaseModel, Field

from sirius_brain.protocol import EventLevel


class ReplayEntry(BaseModel):
    """单个回放条目：一条事件 + 调度信息。"""

    event: str
    data: dict = Field(default_factory=dict)
    level: EventLevel | None = None
    delay_ms: float = Field(default=0, ge=0)
    timestamp: float | None = None

    @classmethod
    def from_wire(cls, obj: dict) -> "ReplayEntry":
        """从录制的一整帧 dict 构造（兼容简化行）。"""
        if obj.get("type") not in (None, "notification"):
            raise ValueError(f"仅支持回放 notification 帧，got type={obj.get('type')!r}")
        if "event" not in obj:
            raise ValueError("回放行缺少 event 字段")
        return cls(
            event=obj["event"],
            data=obj.get("data") or {},
            level=obj.get("level"),
            delay_ms=obj.get("delay_ms", 0),
            timestamp=obj.get("timestamp"),
        )


def load_replay(path: str | Path) -> list[ReplayEntry]:
    """加载 JSONL 录制文件：每行一帧，空行与 # 注释行跳过。"""
    entries: list[ReplayEntry] = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                entries.append(ReplayEntry.from_wire(json.loads(stripped)))
            except Exception as exc:  # noqa: BLE001 —— 回放文件错误统一带行号上报
                raise ValueError(f"{path}:{lineno}: 无法解析回放行：{exc}") from exc
    return entries
