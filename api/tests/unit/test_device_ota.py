"""Unit tests for OTA domain logic — pure decision helpers + report application.

DB sessions are faked at the seams (lookup helpers patched); these tests pin the
behaviour invariants from docs/architecture/ota-tech-design.md §2.2 without a
database.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models.device import Device
from models.device_ota_job import DeviceOtaJob


class _Result:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj

    def first(self):
        return self._obj


def _device() -> Device:
    d = Device(id=uuid.uuid4(), user_id=uuid.uuid4(), device_name="Pigugu test",
               hardware_id="aa:bb:cc:dd", binding_status="bound")
    return d


def _fw_version(v: str):
    from models.firmware_version import FirmwareVersion
    fw = FirmwareVersion(id=uuid.uuid4(), board_target="lichuang-dev", version=v,
                         file_key=f"fw/lichuang-dev/{v}.bin", size_bytes=1, sha256="a" * 64,
                         signature="c2ln", visibility="released", released_by="script")
    return fw


def _job(device: Device, force: bool = False) -> DeviceOtaJob:
    return DeviceOtaJob(id=uuid.uuid4(), device_id=device.id,
                        firmware_version_id=uuid.uuid4(), force=force,
                        requested_by="test", status="requested")


# ── pure helpers ──────────────────────────────────────────────────────

def test_version_lt_semver():
    from modules.device.ota import version_lt
    assert version_lt("2.3.0", "2.3.1")
    assert not version_lt("2.3.1", "2.3.1")
    assert not version_lt("2.4.0", "2.3.1")
    assert not version_lt(None, "2.3.1")
    assert not version_lt("weird", "2.3.1")


# ── process_check (firmware pushdown decision) ───────────────────────

@pytest.mark.asyncio
async def test_process_check_delivers_and_notifies_when_target_higher():
    from modules.device import ota
    db = AsyncMock()
    device = _device()
    job = _job(device)
    fw = _fw_version("2.3.1")

    with (
        patch("modules.device.ota.find_active_job", new_callable=AsyncMock) as m_job,
        patch("modules.device.ota.get_firmware_version", new_callable=AsyncMock) as m_fw,
        patch("modules.device.ota.get_s3_presigned_url", return_value="https://s3/x") as m_presign,
    ):
        m_job.return_value = job
        m_fw.return_value = fw
        result, push_job = await ota.process_check(db, device, "2.3.0", None)

    assert push_job is None
    assert result["version"] == "2.3.1"
    assert result["url"].startswith("https://s3/")
    assert result["sha256"] == "a" * 64
    assert result["force"] is False
    assert job.status == "notified"
    m_presign.assert_called_once_with("fw/lichuang-dev/2.3.1.bin")
    # version report persisted on the device row
    assert device.current_firmware_version == "2.3.0"


@pytest.mark.asyncio
async def test_process_check_supersedes_when_reported_already_on_target():
    from modules.device import ota
    db = AsyncMock()
    device = _device()
    job = _job(device)
    fw = _fw_version("2.3.1")

    with (
        patch("modules.device.ota.find_active_job", new_callable=AsyncMock) as m_job,
        patch("modules.device.ota.get_firmware_version", new_callable=AsyncMock) as m_fw,
        patch("modules.device.ota.get_s3_presigned_url", return_value="https://s3/x") as m_presign,
    ):
        m_job.return_value = job
        m_fw.return_value = fw
        result, push_job = await ota.process_check(db, device, "2.3.1", None)

    assert result is None
    assert push_job is None
    assert job.status == "superseded"
    assert job.status_detail == "already_on_target"
    m_presign.assert_not_called()  # no wasteful download on an up-to-date device


@pytest.mark.asyncio
async def test_process_check_marks_succeeded_when_notified_job_reaches_target():
    """A job the device was already working on, now reporting the target, succeeded.

    The device's own d2c "succeeded" report goes MQTT→IoT→webhook and loses the
    race to the direct HTTP check it fires right after rebooting into the new
    image; without this the job would close as superseded and drop out of any
    success counting.
    """
    from modules.device import ota
    db = AsyncMock()
    device = _device()
    job = _job(device)
    job.status = "rebooting"
    fw = _fw_version("2.3.1")

    with (
        patch("modules.device.ota.find_active_job", new_callable=AsyncMock) as m_job,
        patch("modules.device.ota.get_firmware_version", new_callable=AsyncMock) as m_fw,
        patch("modules.device.ota.get_s3_presigned_url", return_value="https://s3/x") as m_presign,
    ):
        m_job.return_value = job
        m_fw.return_value = fw
        result, push_job = await ota.process_check(db, device, "2.3.1", None)

    assert result is None
    assert job.status == "succeeded"
    assert job.status_detail == "confirmed_by_check"
    assert push_job is job  # the caller must push on this active→terminal transition
    m_presign.assert_not_called()  # still no re-download for an up-to-date device


@pytest.mark.asyncio
async def test_process_check_no_active_job_returns_none():
    from modules.device import ota
    db = AsyncMock()
    device = _device()

    with patch("modules.device.ota.find_active_job", new_callable=AsyncMock) as m_job:
        m_job.return_value = None
        result, push_job = await ota.process_check(db, device, "2.3.0", None)

    assert result is None
    assert push_job is None


# ── apply_ota_report (d2c ota.report) ────────────────────────────────

@pytest.mark.asyncio
async def test_apply_report_succeeded_updates_device_and_marks_push():
    from modules.device import ota
    device = _device()
    job = _job(device)
    db = AsyncMock()
    db.execute.return_value = _Result(device)

    with (
        patch("modules.device.ota.find_active_job", new_callable=AsyncMock) as m_job,
        patch("modules.device.ota.select") as m_select,
    ):
        m_job.return_value = job
        m_select.return_value.where.return_value = MagicMock()
        returned = await ota.apply_ota_report(db, "aa:bb:cc:dd", "succeeded", "2.3.1", None)

    assert returned is job
    assert job.status == "succeeded"
    assert getattr(job, "_push_needed", False) is True
    assert device.current_firmware_version == "2.3.1"


@pytest.mark.asyncio
async def test_apply_report_downloading_clamps_progress():
    from modules.device import ota
    device = _device()
    job = _job(device)
    db = AsyncMock()
    db.execute.return_value = _Result(device)

    with (
        patch("modules.device.ota.find_active_job", new_callable=AsyncMock) as m_job,
        patch("modules.device.ota.select") as m_select,
    ):
        m_job.return_value = job
        m_select.return_value.where.return_value = MagicMock()
        await ota.apply_ota_report(db, "aa:bb:cc:dd", "downloading", None, 150)

    assert job.status == "downloading"
    assert job.progress_pct == 100
    assert getattr(job, "_push_needed", False) is False


@pytest.mark.asyncio
async def test_apply_report_no_active_job_is_noop():
    from modules.device import ota
    db = AsyncMock()
    device = _device()
    db.execute.return_value = _Result(device)

    with (
        patch("modules.device.ota.find_active_job", new_callable=AsyncMock) as m_job,
        patch("modules.device.ota.select") as m_select,
    ):
        m_job.return_value = None
        m_select.return_value.where.return_value = MagicMock()
        returned = await ota.apply_ota_report(db, "aa:bb:cc:dd", "succeeded", "2.3.1", None)

    assert returned is None


# ── create_upgrade_job (409 semantics) ───────────────────────────────

@pytest.mark.asyncio
async def test_create_upgrade_job_conflict_when_active_exists():
    from modules.device import ota
    db = AsyncMock()
    device = _device()
    in_flight = _job(device)
    in_flight.status = "downloading"  # already told / executing → real 409

    with (
        patch("modules.device.ota.find_active_job", new_callable=AsyncMock) as m_job,
        patch("modules.device.ota.latest_released", new_callable=AsyncMock),
    ):
        m_job.return_value = in_flight
        with pytest.raises(ValueError) as exc:
            await ota.create_upgrade_job(db, device, None, False, "test")
    assert str(exc.value) == "OTA_JOB_ACTIVE"


@pytest.mark.asyncio
async def test_create_upgrade_job_supersedes_stuck_requested():
    from modules.device import ota
    db = AsyncMock()
    device = _device()
    stuck = _job(device)  # requested, never picked up (offline reservation)
    fw = _fw_version("2.3.1")

    with (
        patch("modules.device.ota.find_active_job", new_callable=AsyncMock) as m_job,
        patch("modules.device.ota.latest_released", new_callable=AsyncMock) as m_latest,
    ):
        m_job.return_value = stuck
        m_latest.return_value = fw
        created = await ota.create_upgrade_job(db, device, None, False, "test")

    assert stuck.status == "superseded"  # replaced, not a blocking 409
    assert created is not None
    assert created.status == "requested"


@pytest.mark.asyncio
async def test_apply_report_succeeded_without_active_job_updates_device():
    from modules.device import ota
    db = AsyncMock()
    device = _device()
    db.execute.return_value = _Result(device)

    with (
        patch("modules.device.ota.find_active_job", new_callable=AsyncMock) as m_job,
        patch("modules.device.ota.select") as m_select,
    ):
        m_job.return_value = None
        m_select.return_value.where.return_value = MagicMock()
        returned = await ota.apply_ota_report(db, "aa:bb:cc:dd", "succeeded", "2.3.1", None)

    assert returned is None
    assert device.current_firmware_version == "2.3.1"


@pytest.mark.asyncio
async def test_create_upgrade_job_no_released_version_raises():
    from modules.device import ota
    db = AsyncMock()
    device = _device()

    with (
        patch("modules.device.ota.find_active_job", new_callable=AsyncMock) as m_job,
        patch("modules.device.ota.latest_released", new_callable=AsyncMock) as m_latest,
    ):
        m_job.return_value = None
        m_latest.return_value = None
        with pytest.raises(ValueError) as exc:
            await ota.create_upgrade_job(db, device, None, False, "test")
    assert str(exc.value) == "OTA_VERSION_NOT_FOUND"
