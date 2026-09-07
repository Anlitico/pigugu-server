"""Internal firmware-version management (upload / publish / retire / list).

Guarded by a shared secret header (settings.ota_internal_secret) — called by
the firmware release script and (later) pigugu-admin. Not exposed to the App.
"""
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import get_db
from models.firmware_version import DRAFT, RELEASED, RETIRED, FirmwareVersion
from modules.device.schemas import FirmwareUploadRequest, FirmwareVersionView

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/ota", tags=["device", "ota"])


async def _require_internal_secret(
    x_ota_internal_secret: str | None = Header(None, alias="X-OTA-Internal-Secret"),
):
    if not settings.ota_internal_secret or x_ota_internal_secret != settings.ota_internal_secret:
        raise HTTPException(status_code=403, detail="FORBIDDEN")
    return x_ota_internal_secret


async def _get_version_or_404(db: AsyncSession, version_id: uuid.UUID) -> FirmwareVersion:
    result = await db.execute(select(FirmwareVersion).where(FirmwareVersion.id == version_id))
    fw = result.scalar_one_or_none()
    if fw is None:
        raise HTTPException(status_code=404, detail="VERSION_NOT_FOUND")
    return fw


@router.post("/versions", response_model=FirmwareVersionView, status_code=201)
async def upload_firmware_version(
    body: FirmwareUploadRequest,
    _secret: str = Depends(_require_internal_secret),
    db: AsyncSession = Depends(get_db),
):
    """Register a new build (draft by default). Idempotent per (board, version)."""
    existing = (
        await db.execute(
            select(FirmwareVersion).where(
                FirmwareVersion.board_target == body.board_target,
                FirmwareVersion.version == body.version,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.sha256 == body.sha256:
            return existing  # re-upload of the same build — treat as no-op
        raise HTTPException(status_code=409, detail="VERSION_EXISTS")

    fw = FirmwareVersion(
        id=uuid.uuid4(),
        board_target=body.board_target,
        version=body.version,
        git_sha=body.git_sha,
        file_key=body.file_key,
        size_bytes=body.size_bytes,
        sha256=body.sha256,
        signature=body.signature,
        release_notes=body.release_notes,
        visibility=DRAFT,
        released_by=body.released_by,
    )
    db.add(fw)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(status_code=409, detail="VERSION_EXISTS")
    return fw


@router.get("/versions", response_model=list[FirmwareVersionView])
async def list_firmware_versions(
    board_target: str | None = Query(None),
    visibility: str | None = Query(None),
    _secret: str = Depends(_require_internal_secret),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(FirmwareVersion).order_by(FirmwareVersion.created_at.desc())
    if board_target:
        stmt = stmt.where(FirmwareVersion.board_target == board_target)
    if visibility:
        stmt = stmt.where(FirmwareVersion.visibility == visibility)
    return list((await db.execute(stmt)).scalars().all())


@router.post("/versions/{version_id}/publish", response_model=FirmwareVersionView)
async def publish_firmware_version(
    version_id: uuid.UUID,
    _secret: str = Depends(_require_internal_secret),
    db: AsyncSession = Depends(get_db),
):
    """draft → released (becomes the 'latest' visible to App + devices)."""
    fw = await _get_version_or_404(db, version_id)
    if fw.visibility != RELEASED:
        fw.visibility = RELEASED
        fw.released_at = datetime.now(timezone.utc)
        fw.released_by = fw.released_by or "script"
    return fw


@router.post("/versions/{version_id}/retire", response_model=FirmwareVersionView)
async def retire_firmware_version(
    version_id: uuid.UUID,
    _secret: str = Depends(_require_internal_secret),
    db: AsyncSession = Depends(get_db),
):
    """Stop pointing new upgrades at this version (in-flight jobs finish)."""
    fw = await _get_version_or_404(db, version_id)
    fw.visibility = RETIRED
    return fw
