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
    """lookAt({ x, y, z })。spec §8.2。"""

    x: float
    y: float
    z: float


class GetGuiStateParams(BaseModel):
    """getGuiState() — widget 树：standard（结构化）/ fallback（矩形+贴图名）。spec §8.2。"""

    pass


class WorldQueryParams(BaseModel):
    """world.query({ type: "blocks"|"entities", range })。spec §8.2。"""

    type: WorldQueryType
    range: float = Field(gt=0)


class GetStatsParams(BaseModel):
    """getStats()。spec §8.2。"""

    pass


class MouseMoveParams(BaseModel):
    """input.mouseMove({ x, y })。spec §8.2。"""

    x: float
    y: float


class ClickParams(BaseModel):
    """input.click({ button, count })。spec §8.2。"""

    button: int
    count: int = Field(default=1, ge=1)


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
