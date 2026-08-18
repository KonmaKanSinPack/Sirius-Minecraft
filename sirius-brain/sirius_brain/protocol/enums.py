"""协议枚举。spec §8.2、§8.4。"""

from enum import StrEnum


class EventLevel(StrEnum):
    """事件分级。spec §8.2：CRITICAL→反射层立即或 L1 中断；WARNING→排队；INFO→缓冲。"""

    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"


class TaskFinishedStatus(StrEnum):
    """NEKO task_finished 五态。spec §8.2。superseded 为中性状态，不算失败。"""

    OK = "ok"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    SUPERSEDED = "superseded"
    TIMEOUT = "timeout"


class InterruptPolicy(StrEnum):
    """中断策略。spec §8.4：取消 PAUSE，保留 CANCEL/DEFLECT。"""

    CANCEL = "cancel"
    DEFLECT = "deflect"


class ScreenshotTier(StrEnum):
    """截图档位。spec §8.2。"""

    FULL = "full"
    CROP = "crop"


class WorldQueryType(StrEnum):
    """world.query 查询类型。spec §8.2。"""

    BLOCKS = "blocks"
    ENTITIES = "entities"
