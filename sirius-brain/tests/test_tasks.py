"""任务卡与报告模型测试。spec §5 / §4.2。"""

import pytest
from pydantic import ValidationError

from sirius_brain.protocol import (
    ReportBlocked,
    ReportDone,
    ReportProgress,
    RequestDecision,
    TaskCard,
)


def make_card(**over):
    d = dict(
        task_id="T-42",
        goal="清理矿井入口的骷髅群",
        success_criteria="nearest skeleton within 32 blocks == null && health > 10",
        constraints=["不许破坏方块", "血量<8立即撤退"],
        tools_allowlist=["!attack", "!goToCoordinates", "!stats", "!inventory"],
        interrupt_policy="deflect",
        timeout_mins=10,
        context=["<相关记忆 top-3>"],
    )
    d.update(over)
    return TaskCard(**d)


class TestTaskCard:
    def test_spec5_example(self):
        """spec §5 任务卡示例逐字段。"""
        card = make_card()
        assert card.task_id == "T-42"
        assert card.timeout_mins == 10
        assert card.interrupt_policy == "deflect"
        assert len(card.constraints) == 2
        assert TaskCard.model_validate(card.model_dump()) == card

    def test_missing_required(self):
        with pytest.raises(ValidationError):
            TaskCard(task_id="T-1", goal="g", success_criteria="s",
                     interrupt_policy="deflect")  # type: ignore[call-arg]  # 缺 timeout_mins

    def test_interrupt_policy_paused_not_allowed(self):
        """spec §8.4：取消 PAUSE，仅剩 cancel/deflect。"""
        with pytest.raises(ValidationError):
            make_card(interrupt_policy="pause")

    def test_timeout_mins_positive(self):
        with pytest.raises(ValidationError):
            make_card(timeout_mins=0)


class TestReports:
    def test_spec5_blocked_report(self):
        r = ReportBlocked(task_id="T-42", reason="骷髅在岩浆后，无法近战",
                          observation="<!stats + !nearbyBlocks 输出>")
        assert r.type == "blocked"
        assert ReportBlocked.model_validate(r.model_dump()) == r

    @pytest.mark.parametrize(
        "model,kwargs",
        [
            (ReportDone, dict(task_id="T-42", result="3 diamonds", evidence="!inventory")),
            (ReportProgress, dict(task_id="T-42", step="smelt", done=2, total=5)),
            (RequestDecision, dict(task_id="T-42", question="撤退吗？",
                                   options=["撤", "战"], default="战")),
        ],
    )
    def test_round_trip(self, model, kwargs):
        inst = model(**kwargs)
        assert model.model_validate(inst.model_dump()) == inst

    def test_request_decision_default_timeout_30s(self):
        r = RequestDecision(task_id="T-1", question="q", options=["a", "b"], default="a")
        assert r.timeout == 30

    def test_missing_required(self):
        with pytest.raises(ValidationError):
            ReportDone(task_id="T-1", result="r")  # type: ignore[call-arg]  # 缺 evidence
