"""任务卡与执行器报告模型。spec §5、§4.2。"""

from typing import Literal

from pydantic import BaseModel, Field

from .enums import InterruptPolicy


class TaskCard(BaseModel):
    """任务卡（规划器 → 执行器）。spec §5。"""

    task_id: str
    goal: str
    success_criteria: str = Field(description="必须可机检，如 'inventory.diamond >= 3'")
    constraints: list[str] = Field(default_factory=list)
    tools_allowlist: list[str] = Field(default_factory=list)
    interrupt_policy: InterruptPolicy
    timeout_mins: int = Field(gt=0)
    context: list[str] = Field(default_factory=list, description="相关记忆/知识/skill 检索 top-3 注入")


class ReportDone(BaseModel):
    """reportDone({ result, evidence })。spec §4.2。"""

    type: Literal["done"] = "done"
    task_id: str
    result: str
    evidence: str


class ReportBlocked(BaseModel):
    """reportBlocked({ reason, observation })。spec §4.2 / §5 报告示例。"""

    type: Literal["blocked"] = "blocked"
    task_id: str
    reason: str
    observation: str


class RequestDecision(BaseModel):
    """requestDecision({ question, options, default, timeout }) — 默认 30s 超时后按 default 继续。spec §4.2。"""

    type: Literal["decision"] = "decision"
    task_id: str
    question: str
    options: list[str]
    default: str
    timeout: int = Field(default=30, gt=0)


class ReportProgress(BaseModel):
    """reportProgress({ step, done, total })。spec §4.2。"""

    type: Literal["progress"] = "progress"
    task_id: str
    step: str
    done: int = Field(ge=0)
    total: int = Field(gt=0)
