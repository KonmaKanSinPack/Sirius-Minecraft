"""信封帧与工具参数测试。spec §8.2。"""

import pytest
from pydantic import ValidationError

from sirius_brain.protocol import (
    CapabilitiesListRequest,
    CapabilitiesListResponse,
    Capability,
    NotificationFrame,
    ScreenshotParams,
    TaskFinishedFrame,
    TaskFinishedStatus,
    TaskFrame,
    TextParams,
    ToolCallError,
    ToolCallRequest,
    ToolCallResponse,
    WorldQueryParams,
)


class TestRoundTrip:
    """model_dump → model_validate 往返相等。"""

    @pytest.mark.parametrize(
        "model,instance",
        [
            (ToolCallRequest, ToolCallRequest(id="1", method="screenshot",
                                              params={"tier": "full"})),
            (ToolCallResponse, ToolCallResponse(id="1", result={"ok": True})),
            (ToolCallResponse, ToolCallResponse(
                id="1", error=ToolCallError(code=-32602, message="bad params"))),
            (NotificationFrame, NotificationFrame(
                event="health", data={"health": 6}, timestamp=1e9, seq=42)),
            (CapabilitiesListRequest, CapabilitiesListRequest(id="2")),
            (CapabilitiesListResponse, CapabilitiesListResponse(
                id="2", result=[Capability(name="screenshot", version="1.0",
                                           input_schema={"type": "object"})],
                protocol_version="1.0")),
            (TaskFrame, TaskFrame(task="挖一组铁矿石", task_id="T-42")),
            (TaskFinishedFrame, TaskFinishedFrame(
                status=TaskFinishedStatus.SUPERSEDED, task_id="T-42", text="被顶替")),
        ],
    )
    def test_round_trip(self, model, instance):
        assert model.model_validate(instance.model_dump()) == instance

    def test_notification_wire_format(self):
        """字段名与线上 JSON 一致：{type,event,data,timestamp,seq}。"""
        d = NotificationFrame(event="chat", data={}, timestamp=1.0, seq=0).model_dump()
        assert set(d) == {"type", "event", "data", "timestamp", "seq"}
        assert d["type"] == "notification"

    def test_json_round_trip(self):
        req = ToolCallRequest(id="9", method="input.text", params={"string": "hi"})
        assert ToolCallRequest.model_validate_json(req.model_dump_json()) == req


class TestValidation:
    def test_request_requires_id_and_method(self):
        with pytest.raises(ValidationError):
            ToolCallRequest(id="1")  # type: ignore[call-arg]

    def test_notification_requires_event_timestamp_seq(self):
        with pytest.raises(ValidationError):
            NotificationFrame(event="chat")  # type: ignore[call-arg]

    def test_capabilities_method_frozen(self):
        with pytest.raises(ValidationError):
            CapabilitiesListRequest(id="1", method="other/list")

    def test_screenshot_tier_invalid(self):
        with pytest.raises(ValidationError):
            ScreenshotParams(tier="huge")

    def test_screenshot_valid(self):
        p = ScreenshotParams(tier="crop", bbox=(0, 0, 100, 100), quality=80)
        assert ScreenshotParams.model_validate(p.model_dump()) == p

    def test_world_query_type_invalid(self):
        with pytest.raises(ValidationError):
            WorldQueryParams(type="chunks", range=32)

    def test_text_missing_string(self):
        with pytest.raises(ValidationError):
            TextParams()  # type: ignore[call-arg]
