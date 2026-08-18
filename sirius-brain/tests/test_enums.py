"""枚举与 NEKO 帧约束测试。spec §8.2。"""

import pytest
from pydantic import ValidationError

from sirius_brain.protocol import EventLevel, TaskFinishedFrame, TaskFinishedStatus


class TestEnums:
    def test_event_levels(self):
        assert {lv.value for lv in EventLevel} == {"CRITICAL", "WARNING", "INFO"}

    def test_task_finished_five_states(self):
        assert {s.value for s in TaskFinishedStatus} == {
            "ok", "failed", "interrupted", "superseded", "timeout",
        }

    @pytest.mark.parametrize("status", list(TaskFinishedStatus))
    def test_all_states_accepted(self, status):
        f = TaskFinishedFrame(status=status, task_id="T-1", text="x")
        assert f.status is status

    def test_invalid_state_rejected(self):
        with pytest.raises(ValidationError):
            TaskFinishedFrame(status="cancelled", task_id="T-1", text="x")


class TestNekoTaskId:
    """NEKO 帧 task_id 必须原样回传。spec §8.2。"""

    def test_task_id_echoed_verbatim(self):
        tid = "3f9c8e7a-1111-2222-3333-abcdef012345"
        sent = TaskFinishedFrame(status="ok", task_id=tid, text="done")
        assert sent.task_id == tid
        echo = TaskFinishedFrame.model_validate(sent.model_dump())
        assert echo.task_id == tid

    def test_task_id_unmodified_through_serialization(self):
        # 疑似 uuid 变体字符串不得被任何层改写/规范化
        tid = "007--leading-zeros-保持原样"
        frame = TaskFinishedFrame(status="timeout", task_id=tid, text="")
        assert TaskFinishedFrame.model_validate_json(frame.model_dump_json()).task_id == tid

    def test_task_id_required(self):
        with pytest.raises(ValidationError):
            TaskFinishedFrame(status="ok", text="done")  # type: ignore[call-arg]
