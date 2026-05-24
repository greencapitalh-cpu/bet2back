import base64
import os
import uuid
from datetime import datetime
from io import BytesIO

try:
    import boto3
except ImportError:  # Optional until R2 variables are configured in production.
    boto3 = None


ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _r2_configured():
    required = [
        "R2_ENDPOINT_URL",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET_NAME",
    ]
    return boto3 is not None and all(os.getenv(key) for key in required)


def upload_image(file_storage, folder="promotions"):
    content_type = file_storage.mimetype or "application/octet-stream"
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise ValueError("Only JPG, PNG, WEBP or GIF images are allowed.")

    extension = ALLOWED_IMAGE_TYPES[content_type]
    key = f"{folder.strip('/')}/{datetime.utcnow():%Y/%m}/{uuid.uuid4().hex}{extension}"
    body = file_storage.read()
    if not body:
        raise ValueError("Image file is empty.")

    if not _r2_configured():
        encoded = base64.b64encode(body).decode("ascii")
        return {
            "url": f"data:{content_type};base64,{encoded}",
            "key": key,
            "storage": "inline-fallback",
        }

    client = boto3.client(
        "s3",
        endpoint_url=os.getenv("R2_ENDPOINT_URL"),
        aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
        region_name=os.getenv("R2_REGION", "auto"),
    )
    bucket = os.getenv("R2_BUCKET_NAME")
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
        CacheControl="public, max-age=31536000, immutable",
    )

    public_base = (os.getenv("R2_PUBLIC_BASE_URL") or "").rstrip("/")
    url = f"{public_base}/{key}" if public_base else f"{os.getenv('R2_ENDPOINT_URL').rstrip('/')}/{bucket}/{key}"
    return {"url": url, "key": key, "storage": "r2"}


def read_image(key):
    if not _r2_configured():
        return None

    client = boto3.client(
        "s3",
        endpoint_url=os.getenv("R2_ENDPOINT_URL"),
        aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
        region_name=os.getenv("R2_REGION", "auto"),
    )
    response = client.get_object(Bucket=os.getenv("R2_BUCKET_NAME"), Key=key)
    return {
        "body": BytesIO(response["Body"].read()),
        "content_type": response.get("ContentType") or "application/octet-stream",
    }
