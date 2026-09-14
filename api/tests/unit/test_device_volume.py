"""Volume plumbing between the device, the cloud and the App.

Covers what the App can rely on: a level that came from the device, a
remembered level marked as stale, "unknown" (never 0) when there is nothing,
and a write that fails loudly when the device does not confirm it.
"""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

import modules.device.iot as iot
import modules.device.service as service
from modules.device.iot import (
    _handle_device_status,
    _handle_volume_ack,
    _handle_volume_changed,
    volume_ack_key,
    volume_from_status,
    volume_key,
    status_key,
)
from modules.device.schemas import DeviceVolumeResponse


class TestVolumeFromStatus:
    def test_reads_the_speaker_level(self):
        assert volume_from_status({"audio_speaker": {"volume": 42}}) == 42

    def test_accepts_a_zero_level(self):
        """0 is a real level (muted), not 'missing'."""
        assert volume_from_status({"audio_speaker": {"volume": 0}}) == 0

    @pytest.mark.parametrize(
        "status",
        [None, "nonsense", {}, {"audio_speaker": None}, {"audio_speaker": {}}, {"audio_speaker": {"volume": "loud"}}],
    )
    def test_returns_none_for_anything_unreadable(self, status):
        assert volume_from_status(status) is None


class TestVolumeChangedHandler:
    async def test_remembers_the_level_and_pushes_it_to_the_app(self):
        with patch.object(iot, "redis_set", AsyncMock()) as setter, patch.object(
            iot, "_push_ws_by_hw", AsyncMock()
        ) as push:
            await _handle_volume_changed("hw1", {"value": 30, "ts": 111})

        key, payload = setter.await_args.args
        assert key == volume_key("hw1")
        assert json.loads(payload) == {"volume": 30, "ts": 111}
        assert push.await_args.args[1]["volume"] == 30

    async def test_a_non_numeric_value_is_dropped_not_stored(self):
        with patch.object(iot, "redis_set", AsyncMock()) as setter, patch.object(
            iot, "_push_ws_by_hw", AsyncMock()
        ) as push:
            await _handle_volume_changed("hw1", {"value": "loud"})

        setter.assert_not_awaited()
        push.assert_not_awaited()


class TestVolumeAckHandler:
    async def test_stores_the_ack_under_the_request_id(self):
        with patch.object(iot, "redis_set", AsyncMock()) as setter:
            await _handle_volume_ack("hw1", {"request_id": "r1", "value": 55})

        calls = {call.args[0]: json.loads(call.args[1]) for call in setter.await_args_list}
        assert calls[volume_ack_key("hw1", "r1")]["value"] == 55
        # An ack is also news about the level, so it refreshes the last known value.
        assert calls[volume_key("hw1")]["volume"] == 55

    async def test_an_ack_without_a_request_id_is_dropped(self):
        with patch.object(iot, "redis_set", AsyncMock()) as setter:
            await _handle_volume_ack("hw1", {"value": 55})

        setter.assert_not_awaited()


class TestDeviceStatusHandler:
    async def test_stores_the_reply_and_learns_the_level(self):
        msg = {"request_id": "r9", "status": {"audio_speaker": {"volume": 20}}, "ts": 5}

        with patch.object(iot, "redis_set", AsyncMock()) as setter, patch.object(
            iot, "_push_ws_by_hw", AsyncMock()
        ) as push:
            await _handle_device_status("hw1", msg)

        keys = [call.args[0] for call in setter.await_args_list]
        assert status_key("hw1", "r9") in keys
        assert volume_key("hw1") in keys
        assert push.await_args.args[1]["volume"] == 20

    async def test_an_unsolicited_status_with_no_level_still_replies(self):
        with patch.object(iot, "redis_set", AsyncMock()) as setter, patch.object(
            iot, "_push_ws_by_hw", AsyncMock()
        ) as push:
            await _handle_device_status("hw1", {"request_id": "r9", "status": {}})

        keys = [call.args[0] for call in setter.await_args_list]
        assert keys == [status_key("hw1", "r9")]
        push.assert_not_awaited()


def _device():
    device = AsyncMock()
    device.hardware_id = "HW1"
    return device


class TestGetDeviceVolume:
    async def test_prefers_a_fresh_answer_from_the_device(self):
        reply = {"status": {"audio_speaker": {"volume": 33}}, "ts": 99}
        with patch.object(iot, "ping_pong", AsyncMock(return_value=reply)):
            result = await service.get_device_volume(_device())

        assert result == DeviceVolumeResponse(volume=33, stale=False, synced_at=99)

    async def test_falls_back_to_the_remembered_level_marked_stale(self):
        with patch.object(iot, "ping_pong", AsyncMock(return_value=None)), patch.object(
            service, "redis_get", AsyncMock(return_value=json.dumps({"volume": 12, "ts": 7}))
        ):
            result = await service.get_device_volume(_device())

        assert result.volume == 12
        assert result.stale is True
        assert result.synced_at == 7

    async def test_reports_unknown_rather_than_zero_when_nothing_is_known(self):
        with patch.object(iot, "ping_pong", AsyncMock(return_value=None)), patch.object(
            service, "redis_get", AsyncMock(return_value=None)
        ):
            result = await service.get_device_volume(_device())

        assert result.volume is None
        assert result.stale is True

    async def test_unreadable_cached_json_does_not_raise(self):
        with patch.object(iot, "ping_pong", AsyncMock(return_value=None)), patch.object(
            service, "redis_get", AsyncMock(return_value="{not json")
        ):
            result = await service.get_device_volume(_device())

        assert result.volume is None


class TestSetDeviceVolume:
    async def test_reports_the_level_the_device_confirmed(self):
        with patch.object(iot, "ping_pong", AsyncMock(return_value={"value": 45, "ts": 3})):
            result = await service.set_device_volume(_device(), 45)

        assert result == DeviceVolumeResponse(volume=45, stale=False, synced_at=3)

    async def test_a_silent_device_raises_instead_of_reporting_success(self):
        with patch.object(iot, "ping_pong", AsyncMock(return_value=None)):
            with pytest.raises(service.DeviceUnreachable):
                await service.set_device_volume(_device(), 45)
