import boto3
import aiofiles
import uuid
import io
from fastapi import UploadFile, HTTPException
from core.config import settings
from models.models import MessageType
from typing import Optional, Tuple
import structlog

logger = structlog.get_logger()

ALLOWED_IMAGES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
ALLOWED_VIDEO  = {"video/mp4", "video/quicktime", "video/webm"}
ALLOWED_AUDIO  = {"audio/mpeg", "audio/ogg", "audio/webm", "audio/mp4"}
ALLOWED_FILES  = {"application/pdf", "text/plain", "application/zip",
                   "application/msword",
                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}

MAX_BYTES = settings.MAX_FILE_SIZE_MB * 1024 * 1024


def _get_s3_client():
    kwargs = dict(
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    )
    if settings.S3_ENDPOINT_URL:
        kwargs["endpoint_url"] = settings.S3_ENDPOINT_URL
    return boto3.client("s3", **kwargs)


async def upload_avatar(file: UploadFile, user_id: str) -> str:
    contents = await file.read()
    if len(contents) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Avatar must be under 5MB")

    key = f"avatars/{user_id}/{uuid.uuid4()}.jpg"
    return await _upload_to_s3(contents, key, file.content_type)


async def upload_media(
    file: UploadFile,
    user_id: str,
) -> Tuple[str, MessageType, int, Optional[int], Optional[str]]:
    """
    Returns: (url, message_type, size_bytes, duration_seconds, thumbnail_url)
    """
    content_type = file.content_type
    contents = await file.read()
    size = len(contents)

    if size > MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"File exceeds {settings.MAX_FILE_SIZE_MB}MB limit")

    if content_type in ALLOWED_IMAGES:
        msg_type = MessageType.IMAGE
        folder = "images"
    elif content_type in ALLOWED_VIDEO:
        msg_type = MessageType.VIDEO
        folder = "videos"
    elif content_type in ALLOWED_AUDIO:
        msg_type = MessageType.AUDIO
        folder = "audio"
    elif content_type in ALLOWED_FILES:
        msg_type = MessageType.FILE
        folder = "files"
    else:
        raise HTTPException(status_code=415, detail=f"Unsupported media type: {content_type}")

    ext = file.filename.rsplit(".", 1)[-1] if "." in file.filename else "bin"
    key = f"media/{user_id}/{folder}/{uuid.uuid4()}.{ext}"
    url = await _upload_to_s3(contents, key, content_type)

    thumbnail = None
    if msg_type == MessageType.IMAGE:
        thumbnail = await _generate_thumbnail(contents, user_id)

    return url, msg_type, size, None, thumbnail


async def _upload_to_s3(contents: bytes, key: str, content_type: str) -> str:
    """Upload bytes to S3 and return public URL."""
    try:
        s3 = _get_s3_client()
        s3.put_object(
            Bucket=settings.AWS_BUCKET_NAME,
            Key=key,
            Body=contents,
            ContentType=content_type,
        )
        if settings.S3_ENDPOINT_URL:
            return f"{settings.S3_ENDPOINT_URL}/{settings.AWS_BUCKET_NAME}/{key}"
        return f"https://{settings.AWS_BUCKET_NAME}.s3.{settings.AWS_REGION}.amazonaws.com/{key}"
    except Exception as e:
        logger.error("S3 upload failed", key=key, error=str(e))
        raise HTTPException(status_code=500, detail="Media upload failed")


async def _generate_thumbnail(image_bytes: bytes, user_id: str, size=(320, 320)) -> Optional[str]:
    """Generate a thumbnail for images using Pillow."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes))
        img.thumbnail(size, Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        buf.seek(0)
        key = f"thumbnails/{user_id}/{uuid.uuid4()}.jpg"
        return await _upload_to_s3(buf.read(), key, "image/jpeg")
    except Exception as e:
        logger.warning("Thumbnail generation failed", error=str(e))
        return None
