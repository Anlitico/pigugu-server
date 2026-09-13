"""Firmware OTA domain logic.

Single source of truth for:
  - what a device check (POST /v1/device/ota) gets told about firmware,
  - the device_ota_jobs lifecycle (created via App/internal trigger, driven by
    the device's ota.report d2c messages and the server-side timeout sweep).

The device's own semver decision (IsNewVersionAvailable) is authoritative for
"should I write the OTA image"; the server only short-circuits a request where
the reported current version already satisfies the job target, so an on-target
device is not re-sent the package on every check. That short-circuit closes the
job as succeeded when it had already reached the device (its slower d2c
"succeeded" report lost the race to the direct HTTP check), and superseded only
when it never started.
"""
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.aws import get_s3_presigned_url
from core.config import settings
from models.device import Device
from models.device_ota_job import (
    ACTIVE_STATUSES,
    REQUESTED,
    NOTIFIED,
    DOWNLOADING,
    INSTALLING,
    REBOOTING,
    SUCCEEDED,
    FAILED,
    ROLLED_BACK,
    SUPERSEDED,
    DeviceOtaJob,
)
from models.firmware_version import RELEASED, FirmwareVersion

logger = logging.getLogger(__name__)

_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def parse_semver(version: str) -> tuple[int, int, int] | None:
    m = _SEMVER_RE.match((version or "").strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def version_lt(a: str | None, b: str | None) -> bool:
    pa, pb = parse_semver(a or ""), parse_semver(b or "")
    if pa is None or pb is None:
        return False
    return pa < pb


# ── lookups ──────────────────────────────────────────────────────────────

async def latest_released(db: AsyncSession, board_target: str | None = None) -> FirmwareVersion | None:
    """Newest visibility='released' firmware for the target board."""
    board_target = board_target or settings.ota_board_target
    result = await db.execute(
        select(FirmwareVersion)
        .where(
            FirmwareVersion.board_target == board_target,
            FirmwareVersion.visibility == RELEASED,
        )
        .order_by(FirmwareVersion.released_at.desc().nulls_last(), FirmwareVersion.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_firmware_version(
    db: AsyncSession, version_id: uuid.UUID, released_only: bool = False
) -> FirmwareVersion | None:
    stmt = select(FirmwareVersion).where(FirmwareVersion.id == version_id)
    if released_only:
        stmt = stmt.where(FirmwareVersion.visibility == RELEASED)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def find_active_job(db: AsyncSession, device_id: uuid.UUID) -> DeviceOtaJob | None:
    result = await db.execute(
        select(DeviceOtaJob)
        .where(DeviceOtaJob.device_id == device_id, DeviceOtaJob.status.in_(ACTIVE_STATUSES))
        .order_by(DeviceOtaJob.requested_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# ── device check (POST /v1/device/ota) ───────────────────────────────────

async def process_check(
    db: AsyncSession,
    device: Device,
    reported_version: str | None,
    reported_git: str | None,
) -> tuple[dict | None, DeviceOtaJob | None]:
    """Record the device's reported version, then decide the firmware block.

    Returns ``(firmware, push_job)``:
    - ``firmware`` is the response dict (version/url/sha256/signature/force)
      when there is an actionable active job, otherwise None.
    - ``push_job`` is a job whose active→terminal transition happened on this
      request and therefore needs an FCM push from the caller (mirrors the
      webhook path, which the direct HTTP check routinely beats in a race).
    """
    now = datetime.now(timezone.utc)
    if reported_version or reported_git:
        device.current_firmware_version = reported_version
        device.current_firmware_sha = reported_git
        device.last_firmware_report_at = now

    job = await find_active_job(db, device.id)
    if job is None:
        return None, None

    fw = await get_firmware_version(db, job.firmware_version_id)
    if fw is None:
        # target disappeared (deleted) — nothing actionable left to push
        await _terminate(db, job, SUPERSEDED, "firmware_version_missing")
        return None, None

    current = reported_version or device.current_firmware_version
    # Short-circuit on the version reported by THIS request (not a stale DB col).
    if not job.force and current is not None and not version_lt(current, fw.version):
        # The device is at/above the target. If the job had been handed to it,
        # that means the upgrade finished: the device's d2c "succeeded" report
        # travels MQTT→IoT→webhook and routinely loses this race to the direct
        # HTTP check the device fires right after rebooting into the new image.
        # Only a job that never reached the device is genuinely superseded.
        if job.status in DEVICE_AWARE_STATUSES:
            await _terminate(db, job, SUCCEEDED, "confirmed_by_check")
            return None, job
        await _terminate(db, job, SUPERSEDED, "already_on_target")
        return None, None

    try:
        url = get_s3_presigned_url(fw.file_key)
    except Exception:
        logger.exception("Failed to presign firmware %s for device %s", fw.id, device.id)
        return None, None

    job.status = NOTIFIED
    await db.flush()
    return {
        "version": fw.version,
        "url": url,
        "sha256": fw.sha256,
        "signature": fw.signature,
        "force": job.force,
    }, None


# ── job creation / upgrade trigger ───────────────────────────────────────

async def create_upgrade_job(
    db: AsyncSession,
    device: Device,
    firmware_version_id: uuid.UUID | None,
    force: bool,
    requested_by: str | None,
) -> DeviceOtaJob:
    """Create a fresh requested job for the device.

    Raises ValueError("OTA_JOB_ACTIVE") only when a job the device has already
    been told about (or is executing) is in flight — the App shows 409. A
    `requested` job that was never picked up (device offline / never checked)
    is superseded so re-triggering works immediately.
    """
    active = await find_active_job(db, device.id)
    if active is not None:
        if active.status != REQUESTED:
            raise ValueError("OTA_JOB_ACTIVE")
        await _terminate(db, active, SUPERSEDED, "replaced")

    if firmware_version_id is not None:
        fw = await get_firmware_version(db, firmware_version_id, released_only=not force)
    else:
        fw = await latest_released(db)
    if fw is None:
        raise ValueError("OTA_VERSION_NOT_FOUND")

    job = DeviceOtaJob(
        id=uuid.uuid4(),
        device_id=device.id,
        firmware_version_id=fw.id,
        force=force,
        requested_by=requested_by,
        status=REQUESTED,
        requested_at=datetime.now(timezone.utc),
    )
    db.add(job)
    try:
        await db.flush()
    except IntegrityError:
        # A concurrent trigger won the race — the partial unique index allows
        # only one active job per device. Surface as the usual "already busy".
        raise ValueError("OTA_JOB_ACTIVE")
    return job


# ── device report (d2c ota.report) ───────────────────────────────────────

async def apply_ota_report(
    db: AsyncSession,
    hw_id: str,
    state: str | None,
    version: str | None,
    progress: int | None,
) -> DeviceOtaJob | None:
    """Advance the device's active job from an ota.report d2c message.

    Caller owns the transaction: after commit it should push a terminal
    notification when the returned job has ``_push_needed`` set (i.e. this
    report caused an active → terminal transition).
    """
    hw_id = hw_id.strip().lower()
    result = await db.execute(select(Device).where(Device.hardware_id.ilike(hw_id)))
    device = result.scalar_one_or_none()
    if device is None:
        logger.info("ota.report from unknown device %s — ignored", hw_id)
        return None

    job = await find_active_job(db, device.id)
    if job is None:
        # A terminal report arriving after the job was already closed (timeout/
        # supersede) should still update the device's running version, otherwise
        # the version column goes stale until the device's next header report.
        if state == SUCCEEDED and version and device.current_firmware_version != version:
            device.current_firmware_version = version
            device.last_firmware_report_at = datetime.now(timezone.utc)
            await db.flush()
        return None  # no active job (already terminal / superseded / never triggered)

    if state == SUCCEEDED:
        await _terminate(db, job, SUCCEEDED, None)
        if version:
            device.current_firmware_version = version
        else:
            fw = await get_firmware_version(db, job.firmware_version_id)
            if fw is not None:
                device.current_firmware_version = fw.version
        device.last_firmware_report_at = datetime.now(timezone.utc)
    elif state in (FAILED, ROLLED_BACK):
        await _terminate(db, job, state, version)
    elif state == DOWNLOADING:
        job.status = DOWNLOADING
        if progress is not None:
            job.progress_pct = min(max(progress, 0), 100)
        await db.flush()
    elif state == INSTALLING:
        job.status = INSTALLING
        await db.flush()
    elif state == REBOOTING:
        job.status = REBOOTING
        await db.flush()
    else:
        logger.warning("ota.report unknown state %r from %s", state, hw_id)
    return job


async def _terminate(db: AsyncSession, job: DeviceOtaJob, status: str, detail: str | None) -> None:
    was_active = job.status in ACTIVE_STATUSES
    job.status = status
    if detail:
        job.status_detail = detail
    job.finished_at = datetime.now(timezone.utc)
    await db.flush()
    # Push only on the active → terminal transition (repeated reports are no-ops).
    if was_active:
        job._push_needed = True  # type: ignore[attr-defined]  # transient flag


# ── timeout sweep (server-side watchdog, no device involvement) ──────────

# Jobs the device has already been told about (notified onward). Only these can
# be considered stuck — a never-notified `requested` job is a reservation for an
# offline device and must survive until the next power-on/check. They are also
# the evidence that an upgrade actually started (see process_check).
DEVICE_AWARE_STATUSES = (NOTIFIED, DOWNLOADING, INSTALLING, REBOOTING)


async def sweep_timed_out_jobs(db: AsyncSession) -> int:
    """Mark notified+ jobs with no update for OTA_JOB_TIMEOUT_SECS as failed(timeout)."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.ota_job_timeout_secs)
    result = await db.execute(
        select(DeviceOtaJob)
        .where(DeviceOtaJob.status.in_(DEVICE_AWARE_STATUSES), DeviceOtaJob.updated_at < cutoff)
    )
    jobs = list(result.scalars().all())
    for job in jobs:
        await _terminate(db, job, FAILED, "timeout")
    await db.commit()
    for job in jobs:
        await push_job_terminal(job.device_id, job.status, job.status_detail)
    return len(jobs)


# ── FCM push on terminal state ───────────────────────────────────────────

async def push_job_terminal(device_id: uuid.UUID, status: str, detail: str | None = None) -> None:
    """Notify the device owner's App tokens that an upgrade reached a terminal state.

    Resolves the bound user from the device; same token model as the existing
    device_ready push (one FCM token per app install, registered per user).
    """
    try:
        from core.database import AsyncSessionLocal
        from modules.device.fcm import send_push

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Device).where(Device.id == device_id))
            device = result.scalar_one_or_none()
            if device is None or device.user_id is None:
                return

        title = "固件更新"
        if status == SUCCEEDED:
            title, body = "固件更新完成", "设备已升级到最新版本"
        elif status == FAILED:
            reason = "超时无响应" if detail == "timeout" else "升级失败"
            title, body = "固件升级失败", f"{reason}，可稍后在 App 重试"
        elif status == ROLLED_BACK:
            title, body = "固件升级失败", "设备已自动回滚到上一版本"
        else:
            return

        await send_push(
            device.user_id,
            title,
            body,
            {"type": "firmware", "device_id": str(device_id), "status": status},
        )
    except Exception:
        logger.exception("push_job_terminal failed for device %s", device_id)


# ── App-facing detail / summary ──────────────────────────────────────────

async def build_firmware_summary(db: AsyncSession, device: Device) -> dict:
    """Compact firmware block attached to GET /devices list items (card rendering)."""
    current = device.current_firmware_version
    job = await find_active_job(db, device.id)
    if job is not None:
        fw = await get_firmware_version(db, job.firmware_version_id)
        return {
            "current_version": current,
            "update_available": True,
            "target_version": fw.version if fw else None,
            "status": job.status,
            "progress_pct": job.progress_pct,
        }
    latest = await latest_released(db)
    if latest is not None and (current is None or version_lt(current, latest.version)):
        return {
            "current_version": current,
            "update_available": True,
            "target_version": latest.version,
            "status": None,
            "progress_pct": None,
        }
    return {"current_version": current, "update_available": False,
            "target_version": None, "status": None, "progress_pct": None}


async def _most_recent_job(db: AsyncSession, device_id: uuid.UUID) -> DeviceOtaJob | None:
    result = await db.execute(
        select(DeviceOtaJob)
        .where(DeviceOtaJob.device_id == device_id)
        .order_by(DeviceOtaJob.requested_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _firmware_by_version(db: AsyncSession, version: str) -> FirmwareVersion | None:
    result = await db.execute(
        select(FirmwareVersion).where(
            FirmwareVersion.board_target == settings.ota_board_target,
            FirmwareVersion.version == version,
        )
    )
    return result.scalar_one_or_none()


async def _detail_payload(db: AsyncSession, device: Device, target: str | None,
                          status: str | None, progress_pct: int | None,
                          force: bool = False) -> dict:
    if target is None:
        return {"current_version": device.current_firmware_version, "update": None}
    fw = await _firmware_by_version(db, target)
    return {
        "current_version": device.current_firmware_version,
        "update": {
            "target_version": target,
            "release_notes": fw.release_notes if fw else None,
            "published_at": fw.released_at if fw else None,
            "force": force,
            "status": status,
            "progress_pct": progress_pct,
        },
    }


async def get_device_firmware_detail(db: AsyncSession, device: Device) -> dict:
    """GET /v1/device/{id}/firmware response body.

    Surfaces more than the banner summary: when no job is in flight it still
    reports the last terminal outcome (failed/rolled_back) as long as the
    attempted target is still the pending latest, so the upgrade page can show
    failure + retry instead of an endless connecting spinner.
    """
    current = device.current_firmware_version
    job = await find_active_job(db, device.id)
    if job is not None:
        fw = await get_firmware_version(db, job.firmware_version_id)
        return await _detail_payload(
            db, device,
            fw.version if fw else None, job.status, job.progress_pct,
            force=job.force,
        )

    latest = await latest_released(db)
    if latest is None or (current is not None and not version_lt(current, latest.version)):
        return {"current_version": current, "update": None}  # already latest

    recent = await _most_recent_job(db, device.id)
    if recent is not None and recent.status in (FAILED, ROLLED_BACK):
        recent_fw = await get_firmware_version(db, recent.firmware_version_id)
        if recent_fw is not None and recent_fw.version == latest.version:
            return await _detail_payload(
                db, device, latest.version, recent.status, None,
            )
    return await _detail_payload(db, device, latest.version, None, None)
