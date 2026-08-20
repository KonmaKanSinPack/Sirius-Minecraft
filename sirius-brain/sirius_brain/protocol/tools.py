"""Bridge Mod 工具参数模型。spec §8.2 能力集。"""

from pydantic import BaseModel, Field

from .enums import EventLevel, ScreenshotTier, WorldQueryType


class ScreenshotParams(BaseModel):
    """screenshot({ tier: "full"|"crop", bbox?, quality }) — 它亲眼所见。spec §8.2。"""

    tier: ScreenshotTier
    bbox: tuple[float, float, float, float] | None = None
    quality: int | None = Field(default=None, ge=0, le=100)


class LookParams(BaseModel):
    """look({ yaw, pitch })。spec §8.2。"""

    yaw: float = Field(ge=-180.0, le=180.0)
    pitch: float = Field(ge=-90.0, le=90.0)


class LookAtParams(BaseModel):
    """lookAt({ x, y, z, turn_speed_deg_s? })。spec §8.2；turn_speed_deg_s 为 M3.5 v1.2 增强。

    turn_speed_deg_s（30..720 度/秒）有值时平滑转头：固定角速度插值到目标朝向
    （yaw/pitch 同速推进、先到的轴等待，误差 <1° 收口精确落位），期间新的 look
    调用会替换目标；缺省 = 瞬间转（v1.0/v1.1 行为，完全向后兼容）。
    """

    x: float
    y: float
    z: float
    turn_speed_deg_s: float | None = Field(default=None, ge=30.0, le=720.0)


class DigParams(BaseModel):
    """dig({ x, y, z, timeout_ms? })。M3.5 v1.2 智能挖掘原语（bridge 侧执行）。

    bridge 平滑瞄准（300 deg/s）→ 监视型按住左键 → 主信号（方块变化）判
    broken/timeout/not_digging；挖前安全检查（邻格液体/上方 falling block）拒绝
    blocked_liquid/blocked_falling；已空返回 already_air（幂等成功）。
    timeout_ms 600..30000，缺省 15000。
    """

    x: int
    y: int
    z: int
    timeout_ms: int | None = Field(default=None, ge=600, le=30000)


class GetGuiStateParams(BaseModel):
    """getGuiState() — widget 树：standard（结构化）/ fallback（矩形+贴图名）。spec §8.2。"""

    pass


class WorldQueryParams(BaseModel):
    """world.query({ type, range, filter? })。spec §8.2；filter 为 M3.5 v1.1 增强。

    filter 条目为方块 registry 名（``spruce_log`` 自动补 ``minecraft:`` 前缀）
    或 ``#tag``（``#minecraft:logs`` / 短写 ``#logs``）；缺省 = 不过滤（v1.0 行为）。
    """

    type: WorldQueryType
    range: float = Field(gt=0)
    filter: list[str] | None = None


class GetStatsParams(BaseModel):
    """getStats()。spec §8.2。"""

    pass


class MouseMoveParams(BaseModel):
    """input.mouseMove({ x, y })。spec §8.2。"""

    x: float
    y: float


class ClickParams(BaseModel):
    """input.click({ button, count, hold_ms? })。spec §8.2；hold_ms 为 M3.5 v1.1 增强。

    hold_ms（0..10000）有值时按下→等待→抬起（挖掘式长按）；缺省 = 25ms tap 不变。
    """

    button: int
    count: int = Field(default=1, ge=1)
    hold_ms: int | None = Field(default=None, ge=0, le=10000)


class KeyParams(BaseModel):
    """input.key({ code, duration_ms, modifiers })。spec §8.2。"""

    code: int
    duration_ms: int = Field(default=0, ge=0)
    modifiers: list[str] = Field(default_factory=list)


class TextParams(BaseModel):
    """input.text({ string })。spec §8.2。"""

    string: str


class EventsSubscribeParams(BaseModel):
    """events.subscribe({ types: [...], min_level })。spec §8.2。"""

    types: list[str]
    min_level: EventLevel | None = None


class EventsWatchParams(BaseModel):
    """events.watch({ stat, condition, hysteresis, cooldown_ms })。spec §8.2。"""

    stat: str
    condition: str
    hysteresis: float | None = None
    cooldown_ms: int = Field(ge=0)


# 方法名 → 参数模型 的注册表（JSON-Schema 校验入口）
TOOL_PARAMS: dict[str, type[BaseModel]] = {
    "screenshot": ScreenshotParams,
    "look": LookParams,
    "lookAt": LookAtParams,
    "dig": DigParams,
    "getGuiState": GetGuiStateParams,
    "world.query": WorldQueryParams,
    "getStats": GetStatsParams,
    "input.mouseMove": MouseMoveParams,
    "input.click": ClickParams,
    "input.key": KeyParams,
    "input.text": TextParams,
    "events.subscribe": EventsSubscribeParams,
    "events.watch": EventsWatchParams,
}
